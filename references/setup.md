# One-time setup

Requires Python 3.10+ and `keyring` for stored credentials. Use an existing environment, preferably `codex_env`. If needed, install into that environment:

```text
conda run -n codex_env python -m pip install -r <skill-directory>/requirements.txt
```

In a local interactive terminal, run:

```text
python <skill-directory>/scripts/mail.py auth --user you@your-domain.com
```

Enter Alibaba's **third-party client security password / authorization code** at the hidden prompt. The command verifies login, saves the credential in the OS credential store (Windows Credential Manager on Windows), and stores nonsecret host/port/account settings in `%LOCALAPPDATA%/Codex/alibaba-mail/config.json`. On other platforms the settings directory is `~/.config/Codex/alibaba-mail`. A working OS keyring is required for saved credentials.

Future agent runs under the same OS user retrieve the credential automatically. `status` checks availability without logging in; `check` tests authenticated access. After a password change, run `auth` again. No password is written to the skill folder.

For an environment-managed setup, the script accepts `ALIBABA_MAIL_USER`, `ALIBABA_MAIL_HOST`, and `ALIBABA_MAIL_PASSWORD`; the password environment variable takes precedence over the credential store. A variable set in one shell is not automatically available to unrelated or already-running agent processes. Persistent OS credentials avoid that problem. Do not put secrets in shell history or committed `.env` files.

Default endpoint: `imap.qiye.aliyun.com`, port `993`, certificate-verified SSL/TLS. Override `--host`/`--port` on the command if the working mail client uses another Alibaba endpoint. Ensure the account has IMAP/client access enabled. Authentication failure stops the command; resolve the credential or permissions instead of repeatedly retrying.

Official sources: [Alibaba server settings](https://help.aliyun.com/en/document_detail/36576.html), [client setup](https://help.aliyun.com/en/document_detail/36596.html).
