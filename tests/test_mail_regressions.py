import contextlib
import importlib.util
import imaplib
import io
import json
import unittest
from email.message import EmailMessage
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mail.py"
spec = importlib.util.spec_from_file_location("mail_regressions", SCRIPT)
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)


def header_bytes(raw):
    for separator in (b"\r\n\r\n", b"\n\n"):
        head, found, _ = raw.partition(separator)
        if found:
            return head + found
    raise AssertionError("Synthetic message has no header/body separator")


def message_record(subject, internaldate, body="message body"):
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "reader@example.test"
    message["Subject"] = subject
    message.set_content(body)
    return {"raw": message.as_bytes(), "internaldate": internaldate}


def folder(messages, uidvalidity=10):
    return {"messages": messages, "uidvalidity": uidvalidity}


class SyntheticIMAP:
    """Small IMAP model that distinguishes header and full-body fetches."""

    def __init__(self, folders, reject_address_search=False):
        self.folders = folders
        self.current_folder = None
        self.reject_address_search = reject_address_search
        self.search_calls = []
        self.fetch_calls = []
        self.selected = []

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "' + name.encode("ascii") + b'"'
            for name in self.folders
        ]

    def select(self, encoded_folder, readonly=False):
        name = encoded_folder.decode("ascii").strip('"')
        self.current_folder = name
        self.selected.append((name, readonly))
        return "OK", [str(len(self.folders[name]["messages"])).encode("ascii")]

    def response(self, name):
        return name, [str(self.folders[self.current_folder]["uidvalidity"]).encode("ascii")]

    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            self.search_calls.append(args)
            tokens = {str(value).upper() for value in args if value is not None}
            if self.reject_address_search and {"FROM", "TO", "CC", "BCC"} & tokens:
                raise imaplib.IMAP4.error("server rejects address search")
            uids = self.folders[self.current_folder]["messages"]
            return "OK", [b" ".join(str(uid).encode("ascii") for uid in uids)]

        if command.upper() != "FETCH":
            raise AssertionError(f"Unexpected IMAP command: {command}")

        uid_set, fields = args
        uids = [int(uid) for uid in str(uid_set).split(",")]
        self.fetch_calls.append({"uids": uids, "fields": str(fields)})
        data = []
        for uid in uids:
            record = self.folders[self.current_folder]["messages"][uid]
            headers_only = "HEADER" in str(fields)
            payload = header_bytes(record["raw"]) if headers_only else record["raw"]
            section = "BODY[HEADER]" if headers_only else "BODY[]"
            prefix = (
                f'1 (UID {uid} INTERNALDATE "{record["internaldate"]}" '
                f"{section} {{{len(payload)}}}"
            ).encode("ascii")
            data.extend([(prefix, payload), b")"])
        return "OK", data


def run_json(client, arguments):
    args = mail.parser_for({"user": "reader@example.test"}).parse_args(arguments)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        if args.command == "read":
            status = mail.run_read(client, args)
        else:
            status = mail.run_search_or_export(client, args)
    return status, json.loads(output.getvalue())


def chronology_client():
    return SyntheticIMAP({
        "INBOX": folder({
            100: message_record("inbox-oldest", "01-Oct-2026 08:00:00 +0000"),
            101: message_record("inbox-middle", "02-Oct-2026 08:00:00 +0000"),
        }),
        "Sent": folder({
            3: message_record("sent-latest", "04-Oct-2026 08:00:00 +0000"),
            4: message_record("sent-next", "03-Oct-2026 08:00:00 +0000"),
        }),
    })


class MailRegressionTests(unittest.TestCase):
    def test_fetch_messages_exposes_internaldate_in_optional_metadata(self):
        client = SyntheticIMAP({
            "INBOX": folder({
                7: message_record("dated", "08-Oct-2026 12:34:56 +0800"),
            }),
        })
        client.select(b'"INBOX"', readonly=True)
        metadata = {}

        found = mail.fetch_messages(client, [7], headers_only=True, metadata=metadata)

        self.assertEqual(found[7], header_bytes(client.folders["INBOX"]["messages"][7]["raw"]))
        self.assertEqual(metadata, {7: {"received_at": "2026-10-08T12:34:56+08:00"}})
        self.assertIn("INTERNALDATE", client.fetch_calls[0]["fields"])

    def test_received_sort_orders_all_folders_before_applying_limit(self):
        status, newest = run_json(chronology_client(), [
            "search", "--all-folders", "--sort", "received-desc", "--limit", "2",
        ])
        self.assertEqual(status, 0)
        self.assertEqual([item["subject"] for item in newest["results"]], [
            "sent-latest", "sent-next",
        ])
        self.assertEqual(newest["folders"], ["INBOX", "Sent"])

        status, oldest = run_json(chronology_client(), [
            "search", "--all-folders", "--sort", "received-asc", "--limit", "2",
        ])
        self.assertEqual(status, 0)
        self.assertEqual([item["subject"] for item in oldest["results"]], [
            "inbox-oldest", "inbox-middle",
        ])

    def test_default_search_keeps_folder_then_uid_desc_order(self):
        status, result = run_json(chronology_client(), ["search", "--all-folders", "--limit", "0"])

        self.assertEqual(status, 0)
        self.assertEqual([item["subject"] for item in result["results"]], [
            "inbox-middle", "inbox-oldest", "sent-next", "sent-latest",
        ])

    def test_headers_only_read_fetches_no_body_and_emits_received_at(self):
        client = SyntheticIMAP({
            "INBOX": folder({
                7: message_record("private", "08-Oct-2026 12:34:56 +0800", "SECRET BODY"),
            }),
        })

        status, result = run_json(client, [
            "read", "--uid", "7", "--uidvalidity", "10", "--headers-only",
        ])

        self.assertEqual(status, 0)
        self.assertEqual(result["received_at"], "2026-10-08T12:34:56+08:00")
        self.assertEqual(len(client.fetch_calls), 1)
        self.assertIn("HEADER", client.fetch_calls[0]["fields"])
        self.assertNotIn("BODY.PEEK[]", client.fetch_calls[0]["fields"])
        self.assertNotIn("SECRET BODY", json.dumps(result))

    def test_text_search_matches_visible_html_alternative(self):
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "reader@example.test"
        message["Subject"] = "contract"
        message.set_content("See the HTML version for details.")
        message.add_alternative("<p>合同唯一短语</p>", subtype="html")
        client = SyntheticIMAP({
            "INBOX": folder({
                7: {"raw": message.as_bytes(), "internaldate": "08-Oct-2026 12:34:56 +0800"},
            }),
        })

        status, result = run_json(client, ["search", "--text", "合同唯一短语", "--limit", "0"])

        self.assertEqual(status, 0)
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("合同唯一短语", result["results"][0]["snippet"])

    def test_find_uids_retries_date_only_after_address_search_error(self):
        client = SyntheticIMAP({
            "INBOX": folder({
                7: message_record("candidate", "08-Oct-2026 12:34:56 +0800"),
            }),
        }, reject_address_search=True)
        client.select(b'"INBOX"', readonly=True)

        uids = mail.find_uids(
            client,
            since="2026-10-01",
            before="2026-10-10",
            sender="sender@example.test",
        )

        self.assertEqual(uids, [7])
        self.assertEqual(len(client.search_calls), 2)
        self.assertIn("FROM", {str(value).upper() for value in client.search_calls[0]})
        self.assertNotIn("FROM", {str(value).upper() for value in client.search_calls[1]})

    def test_limit_one_unfiltered_search_fetches_one_header(self):
        messages = {
            uid: message_record(f"message-{uid}", "08-Oct-2026 12:34:56 +0000")
            for uid in range(1, 102)
        }
        client = SyntheticIMAP({"INBOX": folder(messages)})

        status, result = run_json(client, ["search", "--limit", "1"])

        self.assertEqual(status, 0)
        self.assertEqual(result["results"][0]["uid"], "101")
        self.assertEqual(len(client.fetch_calls), 1)
        self.assertEqual(client.fetch_calls[0]["uids"], [101])
        self.assertIn("HEADER", client.fetch_calls[0]["fields"])


if __name__ == "__main__":
    unittest.main()
