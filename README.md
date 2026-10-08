# Alibaba Mail skill

A Codex skill that brings relevant email correspondence and attachments into project work. Search first, read selected messages, and save useful attachments. Full mailbox export is also available.

## Install

Clone this repository directly into your Codex skills directory, so the installed skill folder is also its Git working tree:

```text
git clone https://github.com/billerss11/alibaba-mail.git <your-codex-skills-directory>/alibaba-mail
```

Use Python 3.10+ in your preferred environment. For an existing `codex_env`:

```text
conda run -n codex_env python -m pip install -r <skill-directory>/requirements.txt
```

Run one-time authentication in an interactive terminal using that environment's Python:

```text
python <skill-directory>/scripts/mail.py auth --user you@your-domain.com
```

Enter your Alibaba client security password at the hidden prompt. The password is kept in the OS credential store; nonsecret account settings are stored outside the repository. See [setup](references/setup.md) for environment variables and troubleshooting.

Start a new Codex chat if the new skill has not appeared yet. Use `$alibaba-mail`, or ask a project question involving your Alibaba mailbox.

## Examples

- “Find the supplier's latest delivery commitment for this project.”
- “Find messages from this sender and download their quotation attachments.”
- “Read the message with this subject and help me update the project plan.”
- “Export every folder with all messages and attachments to this local directory.”

```text
python scripts/mail.py search --sender supplier@example.com --subject quotation
python scripts/mail.py read --folder INBOX --uid 123 --uidvalidity 456
python scripts/mail.py read --folder INBOX --uid 123 --headers-only
python scripts/mail.py search --all-folders --subject quotation --sort received-desc --limit 1
python scripts/mail.py read --folder INBOX --uid 123 --output <output-directory>
python scripts/mail.py export --all-folders --output <output-directory>
```

Read [SKILL.md](SKILL.md) for a short question-to-command table. Normal reads return concise text; `read --headers-only` avoids body/attachment downloads, and `read --full` returns all headers and bodies. Exports always preserve the complete raw email, decoded text/HTML, and every attachment. Downloads are never executed. Reuse the `python` interpreter path recorded by authentication to avoid the startup overhead of repeated `conda run` calls.

## Boundaries

The client uses certificate-verified IMAP SSL, opens folders read-only, and fetches with `BODY.PEEK` to preserve read/unread flags. It implements no sending, deleting, moving, or flag changes. It can access only server-retained mail and folders permitted to the account. Bcc is searchable only when present in the stored headers.

Search results report limits/errors. By default, results are grouped by folder with descending UID. `--sort received-desc` or `received-asc` scans all selected folders and sorts before applying the limit. `received_at` is IMAP INTERNALDATE (server receipt/storage time, which can differ for imported mail); `date` remains the sender's Date header. Date filters use the internal date. Body searches check both plain and visible HTML alternatives. If the server rejects optional address narrowing, search retries with date-only criteria and matches decoded headers locally.

Archives use account/folder/UIDVALIDITY/UID identities and skip completed exports on reruns. A live mailbox can change during retrieval; this is not a transactional server backup. Incomplete or conflicting existing output is reported instead of overwritten.

All examples and tests use synthetic mail. Credentials, account settings, downloads, and attachments do not belong in Git; the repository uses an allowlist in `.gitignore` and rejects export destinations inside the skill folder.

## Development

```text
python -B -m unittest discover -s tests -v
```

Tests cover MIME decoding, attachment preservation, safe filenames, Chinese folder names, identity separation, and read-only retrieval. They do not require an account or network connection.
