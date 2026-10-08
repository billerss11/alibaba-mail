import importlib.util
import contextlib
import io
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mail.py"
spec = importlib.util.spec_from_file_location("mail", SCRIPT)
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


def sample_message():
    message = EmailMessage()
    message["From"] = "Supplier <sales@example.test>"
    message["To"] = "buyer@example.test"
    message["Cc"] = "team@example.test"
    message["Subject"] = "合同 invoice"
    message["X-Reference"] = "ORDER-42"
    message.set_content("你好 — complete text")
    message.add_alternative("<p>Complete HTML</p>", subtype="html")
    message.add_attachment(b"first", maintype="application", subtype="octet-stream",
                           filename="../../same.bin")
    message.add_attachment(b"second", maintype="application", subtype="octet-stream",
                           filename="same.bin")
    embedded = EmailMessage()
    embedded["Subject"] = "Attached email"
    embedded.set_content("Nested message body")
    message.add_attachment(embedded, filename="forwarded.eml")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self):
        self.selected = []
        self.commands = []
        self.raw = sample_message()

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\Noselect) "/" "Container"']

    def select(self, folder, readonly=False):
        self.selected.append((folder, readonly))
        return "OK", [b"2"]

    def response(self, name):
        return name, [b"500"]

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command.upper() == "SEARCH":
            return "OK", [b"7 9"]
        if command.upper() == "FETCH":
            result = []
            for uid in str(args[0]).split(","):
                result.extend([(f"1 (UID {uid} BODY[] {{{len(self.raw)}}}".encode(), self.raw), b")"])
            return "OK", result
        raise AssertionError(f"Unexpected IMAP command: {command}")


class MailTests(unittest.TestCase):
    def test_chinese_folder_names_and_quoted_list(self):
        for name in ("INBOX", "合同 & 报价", 'Folder "quoted"', "项目/归档"):
            self.assertEqual(mail.decode_folder(mail.encode_folder(name)), name)
        wire = b'(\\HasNoChildren) "/" ' + mail.quote_folder('Folder "quoted"')
        self.assertEqual(mail.parse_folders([wire])[0]["name"], 'Folder "quoted"')
        literal = (b'(\\HasNoChildren) "/" {5}', b"INBOX")
        self.assertEqual(mail.parse_folders([literal])[0]["name"], "INBOX")

    def test_decoded_filters_include_cc_and_custom_headers(self):
        message = mail.parse_message(sample_message())
        args = SimpleNamespace(sender="SALES@EXAMPLE.TEST", recipient="team@example.test",
                               subject="合同", header=[("X-Reference", "order-42")])
        self.assertTrue(mail.matches(message, args))
        args.recipient = "absent@example.test"
        self.assertFalse(mail.matches(message, args))

    def test_mime_keeps_bodies_binary_attachments_and_embedded_email(self):
        record, attachments = mail.message_content(sample_message())
        self.assertIn("你好", record["text"])
        self.assertIn("Complete HTML", record["html"])
        self.assertEqual(len(attachments), 3)
        self.assertEqual([item[1] for item in attachments[:2]], [b"first", b"second"])
        self.assertIn(b"Nested message body", attachments[2][1])
        self.assertNotIn("Nested message body", record["text"])

    def test_attachment_names_are_safe_unique_and_raw_email_is_preserved(self):
        raw = sample_message()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "message"
            identity = {"folder": "INBOX", "uidvalidity": "500", "uid": "9"}
            mail.save_message(target, raw, identity)
            self.assertEqual((target / "message.eml").read_bytes(), raw)
            metadata = json.loads((target / "message.json").read_text("utf-8"))
            paths = [target / item["path"] for item in metadata["attachments"]]
            self.assertEqual(len(set(paths)), 3)
            self.assertTrue(all(p.resolve().is_relative_to(target.resolve()) for p in paths))
            self.assertTrue(all(p.is_file() for p in paths))
            self.assertTrue(mail.is_saved(target, identity))
            paths[0].unlink()
            self.assertFalse(mail.is_saved(target, identity))

    def test_windows_reserved_names(self):
        for name in ("../CON", "NUL.txt", "..", "a:b?.pdf", "C:\\secret\\file.txt"):
            safe = mail.safe_filename(name)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertNotIn(":", safe)
            self.assertNotIn(safe.split(".")[0].upper(), {"CON", "NUL"})

    def test_search_and_retrieval_use_read_only_and_preserve_uid_identity(self):
        client = FakeIMAP()
        self.assertEqual(mail.select_folder(client, "INBOX"), "500")
        self.assertEqual(mail.find_uids(client, None, None), [7, 9])
        self.assertEqual(mail.fetch_messages(client, [9])[9], client.raw)
        self.assertTrue(all(readonly for _, readonly in client.selected))
        self.assertTrue(all(command.upper() in {"SEARCH", "FETCH"}
                            for command, _ in client.commands))
        self.assertIn("BODY.PEEK[]", client.commands[-1][1][1])

    def test_archive_identity_separates_accounts_folders_and_uidvalidity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = mail.message_path(root, "mail.example.test", "one@example.test", "INBOX", "500", 9)
            b = mail.message_path(root, "mail.example.test", "two@example.test", "INBOX", "500", 9)
            c = mail.message_path(root, "mail.example.test", "one@example.test", "INBOX", "501", 9)
            self.assertEqual(len({a, b, c}), 3)

    def test_exports_cannot_be_written_into_the_skill_repository(self):
        with self.assertRaises(ValueError):
            mail.validate_output(SCRIPT.parent.parent / "downloads")

    def test_search_reports_limits_and_body_matching(self):
        parser = mail.parser_for({"user": "reader@example.test"})
        args = parser.parse_args(["search", "--subject", "合同", "--text", "complete text", "--limit", "1"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = mail.run_search_or_export(FakeIMAP(), args)
        found = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(len(found["results"]), 1)
        self.assertEqual(found["results"][0]["uid"], "9")
        self.assertIn("complete text", found["results"][0]["snippet"])
        self.assertTrue(found["limit_reached"])
        self.assertFalse(found["complete"])

    def test_all_folder_export_can_resume_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            args = mail.parser_for({"user": "reader@example.test"}).parse_args([
                "export", "--all-folders", "--output", temp])
            runs = []
            for _ in range(2):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    result = mail.run_search_or_export(FakeIMAP(), args)
                self.assertEqual(result, 0)
                runs.append(json.loads(output.getvalue()))
            self.assertEqual(runs[0]["saved"], 2)
            self.assertTrue(runs[0]["complete"])
            self.assertEqual(runs[0]["folders"], ["INBOX"])
            self.assertEqual(runs[1]["saved"], 0)
            self.assertEqual(runs[1]["already_saved"], 2)
            self.assertEqual(len(list(Path(temp).rglob("message.eml"))), 2)

    def test_compact_read_declares_truncation_and_omits_large_headers_html(self):
        args = mail.parser_for({"user": "reader@example.test"}).parse_args([
            "read", "--uid", "9", "--uidvalidity", "500", "--max-chars", "5"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mail.run_read(FakeIMAP(), args)
        found = json.loads(output.getvalue())
        self.assertEqual(len(found["body"]), 5)
        self.assertTrue(found["body_truncated"])
        self.assertEqual(len(found["attachments"]), 3)
        self.assertNotIn("headers", found)
        self.assertNotIn("html", found)
        args.uidvalidity = 501
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY"):
            mail.run_read(FakeIMAP(), args)

    def test_html_only_messages_have_readable_text_without_scripts(self):
        message = EmailMessage()
        message.set_content('<html><head><style>SECRET_STYLE</style></head>'
                            '<body><p>Useful &amp; readable</p><script>SECRET_SCRIPT</script></body></html>', subtype="html")
        record, _ = mail.message_content(message.as_bytes())
        body = mail.readable_body(record)
        self.assertIn("Useful & readable", body)
        self.assertNotIn("SECRET", body)


if __name__ == "__main__":
    unittest.main()
