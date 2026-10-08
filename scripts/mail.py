#!/usr/bin/env python3
"""Read-only Alibaba Mail IMAP search, message reading, and attachment export."""

import argparse
import base64
import getpass
import hashlib
import imaplib
import json
import mimetypes
import os
import re
import shutil
import ssl
import sys
import tempfile
import warnings
from contextlib import contextmanager, suppress
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4


SKILL_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "Codex" / "alibaba-mail"
CONFIG_PATH = STATE_DIR / "config.json"


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def credential_service(args):
    return f"codex-alibaba-mail:{args.host.lower()}:{args.port}"


def credential_store():
    try:
        import keyring
    except ImportError as error:
        raise RuntimeError("Install keyring in your Python environment, or set ALIBABA_MAIL_PASSWORD locally.") from error
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
        keyring.set_keyring(WinVaultKeyring())
    return keyring


def get_password(args):
    password = os.environ.get("ALIBABA_MAIL_PASSWORD")
    if not password:
        password = credential_store().get_password(credential_service(args), args.user)
    if not password:
        raise RuntimeError("No saved credential. Run 'mail.py auth --user YOUR_EMAIL' in a local terminal first.")
    return password


@contextmanager
def connect(args, password=None):
    client = imaplib.IMAP4_SSL(args.host, args.port,
                              ssl_context=ssl.create_default_context(), timeout=args.timeout)
    try:
        client.login(args.user, password if password is not None else get_password(args))
        yield client
    finally:
        with suppress(OSError, imaplib.IMAP4.error):
            client.logout()


def encode_folder(name):
    """IMAP modified UTF-7, used by IMAP4rev1 servers for non-ASCII folders."""
    def encode_run(match):
        encoded = base64.b64encode(match.group().encode("utf-16-be")).decode("ascii")
        return "&" + encoded.rstrip("=").replace("/", ",") + "-"
    escaped = name.replace("&", "&-")
    return re.sub(r"[^\x20-\x7e]+", encode_run, escaped).encode("ascii")


def decode_folder(value):
    def decode_run(match):
        payload = match.group(1)
        if not payload:
            return "&"
        payload = payload.replace(",", "/")
        return base64.b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", decode_run, value.decode("ascii"))


def quote_folder(name):
    encoded = encode_folder(name)
    return b'"' + encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def parse_folders(rows):
    folders = []
    for row in rows:
        if row is None:
            continue
        prefix = row[0] if isinstance(row, tuple) else row
        match = re.match(rb'^\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$', prefix)
        if not match:
            raise RuntimeError(f"Could not parse IMAP folder listing: {prefix!r}")
        flags = match[1].decode("ascii").split()
        name = row[1] if isinstance(row, tuple) else match[2]
        if not isinstance(row, tuple) and name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
        folders.append({"name": decode_folder(name), "flags": flags,
                        "selectable": "\\noselect" not in [flag.lower() for flag in flags]})
    return folders


def list_folders(client):
    status, data = client.list()
    if status != "OK":
        raise RuntimeError("The server did not list folders.")
    return parse_folders(data)


def select_folder(client, folder):
    status, _ = client.select(quote_folder(folder), readonly=True)
    if status != "OK":
        raise RuntimeError(f"Cannot open folder: {folder}")
    _, data = client.response("UIDVALIDITY")
    if not data or not data[0] or not data[0].isdigit():
        raise RuntimeError(f"No UIDVALIDITY returned for {folder}; cannot identify messages reliably.")
    return data[0].decode("ascii")


def imap_date(value):
    parsed = date.fromisoformat(value)
    month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[parsed.month - 1]
    return f"{parsed.day:02d}-{month}-{parsed.year}"


def find_uids(client, since, before, sender=None, recipient=None):
    criteria = ["ALL"]
    if since:
        criteria.extend(["SINCE", imap_date(since)])
    if before:
        criteria.extend(["BEFORE", imap_date(before)])
    # Let the server narrow ASCII addresses quickly; decode other header filters locally.
    for name, value in (("FROM", sender), ("RECIPIENT", recipient)):
        if value and re.fullmatch(r"[A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+", value):
            quoted = '"' + value + '"'
            if name == "FROM":
                criteria.extend(["FROM", quoted])
            else:
                criteria.extend(["OR", "TO", quoted, "OR", "CC", quoted, "BCC", quoted])
    status, data = client.uid("SEARCH", None, *criteria)
    if status != "OK":
        raise RuntimeError("The server could not search this folder.")
    return [int(uid) for uid in (data[0] or b"").split()]


def fetch_messages(client, uids, headers_only=False):
    fields = "(UID BODY.PEEK[HEADER])" if headers_only else "(UID BODY.PEEK[])"
    status, data = client.uid("FETCH", ",".join(map(str, uids)), fields)
    if status != "OK":
        raise RuntimeError("The server could not fetch the requested message(s).")
    found = {}
    requested = set(uids)
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            match = re.search(rb"\bUID\s+(\d+)\b", item[0])
            if match and int(match[1]) in requested:
                found[int(match[1])] = item[1]
    return found


def parse_message(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def header_text(message, name):
    return "\n".join(str(value) for value in message.get_all(name, []))


def matches(message, args):
    checks = [(args.sender, header_text(message, "From")),
              (args.recipient, "\n".join(header_text(message, key) for key in ("To", "Cc", "Bcc"))),
              (args.subject, header_text(message, "Subject"))]
    checks.extend((value, header_text(message, name)) for name, value in (args.header or []))
    return all(needle is None or needle.casefold() in haystack.casefold() for needle, haystack in checks)


def summary(message):
    return {name.lower(): header_text(message, name)
            for name in ("From", "To", "Cc", "Bcc", "Subject", "Date", "Message-ID")}


def safe_filename(name):
    leaf = re.split(r"[/\\]", name or "attachment")[-1]
    leaf = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", leaf).strip(" .")[:100].rstrip(" .")
    leaf = leaf or "attachment"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", leaf, re.IGNORECASE):
        leaf = "_" + leaf
    return leaf


def message_content(raw):
    message = parse_message(raw)
    record = summary(message)
    record["headers"] = [[name, str(value)] for name, value in message.items()]
    record.update(text="", html="", attachments=[])
    attachments = []

    def visit(part):
        content_type = part.get_content_type()
        filename = part.get_filename()
        is_attachment = (filename is not None or part.get_content_disposition() == "attachment"
                         or part.get_content_maintype() == "message")
        if part.is_multipart() and not is_attachment:
            for child in part.iter_parts():
                visit(child)
        elif content_type in ("text/plain", "text/html") and not is_attachment:
            try:
                text = part.get_content()
            except LookupError:
                text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
            key = "text" if content_type == "text/plain" else "html"
            record[key] += text + "\n"
        else:
            payload = part.get_payload(decode=True)
            if payload is None:
                children = part.get_payload()
                payload = (b"\r\n".join(child.as_bytes() for child in children)
                           if content_type == "message/rfc822" and isinstance(children, list)
                           else part.as_bytes())
            extension = ".eml" if content_type == "message/rfc822" else mimetypes.guess_extension(content_type) or ".bin"
            filename = filename or "attachment" + extension
            saved_name = f"{len(attachments) + 1:03d}-{safe_filename(filename)}"
            info = {"filename": filename, "content_type": content_type, "size": len(payload),
                    "content_id": str(part.get("Content-ID", "")), "path": "attachments/" + saved_name}
            record["attachments"].append(info)
            attachments.append((saved_name, payload))

    visit(message)
    return record, attachments


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if tag in ("p", "br", "div", "tr", "li"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def readable_body(record):
    if record["text"].strip():
        return record["text"]
    parser = PlainHTML()
    parser.feed(record["html"])
    return "".join(parser.chunks)


def compact_record(record, max_chars):
    body = readable_body(record)
    return {key: value for key, value in record.items() if key not in ("headers", "text", "html")} | {
        "body": body[:max_chars], "body_characters": len(body),
        "body_truncated": len(body) > max_chars,
    }


def validate_output(path):
    resolved = Path(path).expanduser().resolve()
    if resolved.is_relative_to(SKILL_ROOT):
        raise ValueError("Mailbox exports must be outside the skill/Git repository.")
    return resolved


def message_path(root, host, user, folder, uidvalidity, uid):
    account_id = hashlib.sha256(f"{host.lower()}\n{user}".encode()).hexdigest()[:16]
    folder_id = hashlib.sha256(folder.encode()).hexdigest()[:12]
    return root / account_id / (safe_filename(folder)[:35] + "-" + folder_id) / str(uidvalidity) / str(uid)


def is_saved(target, identity):
    try:
        metadata = json.loads((target / "message.json").read_text(encoding="utf-8"))
        if any(str(metadata.get(key)) != str(value) for key, value in identity.items()):
            return False
        files = ["message.eml", "body.txt", "body.html"] + [item["path"] for item in metadata["attachments"]]
        return all((target / name).resolve().is_relative_to(target.resolve()) and (target / name).is_file()
                   for name in files)
    except (OSError, ValueError, KeyError):
        return False


def save_message(target, raw, identity):
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing export: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".mail-tmp-", dir=target.parent))
    try:
        record, attachments = message_content(raw)
        record.update(identity)
        (stage / "message.eml").write_bytes(raw)
        (stage / "body.txt").write_text(record["text"], encoding="utf-8")
        (stage / "body.html").write_text(record["html"], encoding="utf-8")
        if attachments:
            (stage / "attachments").mkdir()
        for name, payload in attachments:
            (stage / "attachments" / name).write_bytes(payload)
        (stage / "message.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        stage.rename(target)
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def iter_matches(client, args, stats):
    folders = ([entry["name"] for entry in list_folders(client) if entry["selectable"]]
               if args.all_folders else (args.folder or ["INBOX"]))
    needs_headers = args.command == "search" or any((args.sender, args.recipient, args.subject, args.header, args.text))
    for folder in dict.fromkeys(folders):
        print(f"Scanning folder: {folder}", file=sys.stderr)
        try:
            validity = select_folder(client, folder)
            uids = list(reversed(find_uids(client, args.since, args.before, args.sender, args.recipient)))
        except imaplib.IMAP4.abort:
            raise
        except (RuntimeError, imaplib.IMAP4.error) as error:
            stats["errors"].append(str(error))
            continue
        stats["folders"].append(folder)
        stats["candidates"] += len(uids)
        for start in range(0, len(uids), 100):
            batch = uids[start:start + 100]
            headers = fetch_messages(client, batch, headers_only=True) if needs_headers else {}
            for uid in batch:
                record = {}
                if needs_headers:
                    if uid not in headers:
                        stats["errors"].append(f"{folder} UID {uid}: missing or moved during search")
                        continue
                    stats["headers_scanned"] += 1
                    message = parse_message(headers[uid])
                    if not matches(message, args):
                        continue
                    record = summary(message)
                if args.text:
                    raw = fetch_messages(client, [uid]).get(uid)
                    if raw is None:
                        stats["errors"].append(f"{folder} UID {uid}: missing or moved during body search")
                        continue
                    content, _ = message_content(raw)
                    body = readable_body(content)
                    position = body.casefold().find(args.text.casefold())
                    if position < 0:
                        continue
                    record["snippet"] = body[max(0, position - 80):position + 160]
                record.update(folder=folder, uidvalidity=validity, uid=str(uid))
                stats["matches"] += 1
                yield record
                if args.limit and stats["matches"] >= args.limit:
                    stats["limit_reached"] = True
                    return


def run_search_or_export(client, args):
    stats = {"folders": [], "candidates": 0, "headers_scanned": 0,
             "matches": 0, "limit_reached": False, "errors": []}
    if args.command == "search":
        results = list(iter_matches(client, args, stats))
        emit({"results": results, **stats,
              "complete": not stats["errors"] and not stats["limit_reached"]})
        return 1 if stats["errors"] else 0

    root = validate_output(args.output)
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    manifest = root / f"manifest-{run_id}.jsonl"
    saved = skipped = 0
    with manifest.open("x", encoding="utf-8") as log:
        try:
            for record in iter_matches(client, args, stats):
                identity = {key: record[key] for key in ("folder", "uidvalidity", "uid")}
                target = message_path(root, f"{args.host}:{args.port}", args.user,
                                      record["folder"], record["uidvalidity"], record["uid"])
                if not target.resolve().is_relative_to(root):
                    raise ValueError("Export path points outside the output directory.")
                if is_saved(target, identity):
                    skipped += 1
                    outcome = "already_saved"
                else:
                    raw = fetch_messages(client, [int(record["uid"])]).get(int(record["uid"]))
                    if raw is None:
                        stats["errors"].append(f"{record['folder']} UID {record['uid']}: missing or moved during export")
                        continue
                    save_message(target, raw, identity)
                    saved += 1
                    outcome = "saved"
                log.write(json.dumps({**identity, "directory": str(target), "status": outcome}, ensure_ascii=False) + "\n")
                log.flush()
                if (saved + skipped) % 25 == 0:
                    print(f"Exported {saved}; already saved {skipped}.", file=sys.stderr)
        except (OSError, RuntimeError, ValueError, imaplib.IMAP4.error) as error:
            stats["errors"].append(str(error))
    result = {"saved": saved, "already_saved": skipped, "manifest": str(manifest),
              **stats, "complete": not stats["errors"] and not stats["limit_reached"]}
    (root / f"summary-{run_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)
    return 1 if stats["errors"] else 0


def run_read(client, args):
    validity = select_folder(client, args.folder)
    if args.uidvalidity and str(args.uidvalidity) != validity:
        raise RuntimeError("Folder UIDVALIDITY changed. Search again before using this UID.")
    raw = fetch_messages(client, [args.uid]).get(args.uid)
    if raw is None:
        raise RuntimeError("Message not found; it may have been moved or deleted by another client.")
    identity = {"folder": args.folder, "uidvalidity": validity, "uid": str(args.uid)}
    record, _ = message_content(raw)
    record.update(identity)
    if args.output:
        root = validate_output(args.output)
        target = message_path(root, f"{args.host}:{args.port}", args.user, args.folder, validity, args.uid)
        if not target.resolve().is_relative_to(root):
            raise ValueError("Export path points outside the output directory.")
        if not is_saved(target, identity):
            save_message(target, raw, identity)
        record["saved_to"] = str(target)
    emit(record if args.full else compact_record(record, args.max_chars))
    return 0


def parser_for(config):
    parser = argparse.ArgumentParser(description=__doc__)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--host", default=os.environ.get("ALIBABA_MAIL_HOST", config.get("host", "imap.qiye.aliyun.com")))
    shared.add_argument("--port", type=int, default=config.get("port", 993))
    shared.add_argument("--user", default=os.environ.get("ALIBABA_MAIL_USER", config.get("user")))
    shared.add_argument("--timeout", type=float, default=30, help="Socket timeout in seconds")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("auth", "Verify and store a client password in the OS credential store"),
                            ("status", "Show local configuration and credential availability"),
                            ("check", "Test authenticated IMAP access"),
                            ("folders", "List all folders exposed by IMAP")):
        commands.add_parser(name, parents=[shared], help=help_text)
    for command in ("search", "export"):
        sub = commands.add_parser(command, parents=[shared], help=f"{command.title()} matching messages")
        scope = sub.add_mutually_exclusive_group()
        scope.add_argument("--folder", action="append", help="Folder name; repeat to select several. Default: INBOX")
        scope.add_argument("--all-folders", action="store_true", help="Every selectable folder, including Sent/Junk/Trash if exposed")
        sub.add_argument("--sender", help="Case-insensitive substring of decoded From headers")
        sub.add_argument("--recipient", help="Substring of To, Cc, or Bcc when present")
        sub.add_argument("--subject", help="Substring of decoded Subject")
        sub.add_argument("--text", help="Decoded message body substring (downloads candidate bodies; narrow other filters first)")
        sub.add_argument("--header", nargs=2, action="append", metavar=("NAME", "VALUE"), help="Custom header substring; repeat for AND")
        sub.add_argument("--since", help="Inclusive server internal date, YYYY-MM-DD")
        sub.add_argument("--before", help="Exclusive server internal date, YYYY-MM-DD")
        sub.add_argument("--limit", type=int, default=10 if command == "search" else 0,
                         help="Maximum matches across selected folders; 0 means unlimited")
        if command == "export":
            sub.add_argument("--output", type=Path, required=True, help="Directory outside this skill/repository")
    read = commands.add_parser("read", parents=[shared], help="Read a complete message and optionally save attachments")
    read.add_argument("--folder", default="INBOX")
    read.add_argument("--uid", type=int, required=True)
    read.add_argument("--uidvalidity", type=int, help="Value from search; rejects stale mailbox identity")
    read.add_argument("--output", type=Path, help="Save raw message, bodies, headers, and all attachments here")
    read.add_argument("--max-chars", type=int, default=8000, help="Maximum body characters in normal read output")
    read.add_argument("--full", action="store_true", help="Return all headers, complete plain text, and HTML")
    return parser


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        config = read_config()
        parser = parser_for(config)
        args = parser.parse_args()
        if not args.user:
            parser.error("Specify --user YOUR_EMAIL or run auth once to save the account configuration.")
        if not 1 <= args.port <= 65535 or args.timeout <= 0:
            parser.error("Port must be 1..65535 and timeout must be positive.")
        if getattr(args, "limit", 0) < 0 or getattr(args, "uid", 1) < 1:
            parser.error("Limit must be nonnegative; UID must be positive.")
        if getattr(args, "max_chars", 1) < 1:
            parser.error("--max-chars must be positive; use --full for unlimited output.")
        for name in ("since", "before"):
            if getattr(args, name, None):
                imap_date(getattr(args, name))
        if getattr(args, "since", None) and args.before and args.since >= args.before:
            parser.error("--since must be earlier than --before.")
        if getattr(args, "output", None):
            validate_output(args.output)
        if args.command == "status":
            source = "environment" if os.environ.get("ALIBABA_MAIL_PASSWORD") else None
            if source is None and credential_store().get_password(credential_service(args), args.user):
                source = "OS credential store"
            emit({"user": args.user, "host": args.host, "port": args.port,
                  "config": str(CONFIG_PATH), "credential_available": source is not None,
                  "credential_source": source, "mailbox_tested": False})
            return 0
        if args.command == "auth":
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Alibaba client security password / authorization code (hidden): ")
            if not password:
                raise ValueError("A client security password is required.")
            with connect(args, password) as client:
                list_folders(client)
            credential_store().set_password(credential_service(args), args.user, password)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps({"user": args.user, "host": args.host, "port": args.port}, indent=2), encoding="utf-8")
            emit({"authenticated": True, "credential_saved": True, "config": str(CONFIG_PATH)})
            return 0
        with connect(args) as client:
            if args.command == "folders":
                emit({"folders": list_folders(client)})
                return 0
            if args.command == "check":
                emit({"authenticated": True, "folders": len(list_folders(client)), "mode": "read-only"})
                return 0
            if args.command == "read":
                return run_read(client, args)
            return run_search_or_export(client, args)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; existing completed exports can be reused on the next run.", file=sys.stderr)
        return 130
    except getpass.GetPassWarning:
        print("Run auth in a local terminal so the password stays hidden.", file=sys.stderr)
        return 1
    except Exception as error:
        # CLI boundary: fail visibly without a traceback or credentials/debug logs.
        emit({"error": str(error), "complete": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
