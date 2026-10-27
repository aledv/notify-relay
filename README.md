# notify-relay

A tiny self-hosted HTTP service that forwards notifications to **Telegram** and/or **email**.

Your scripts, cron jobs and home-lab services POST a message (and optionally a file) to one
endpoint on your LAN; the relay holds the bot token and SMTP credentials, so none of them has
to. Photos and videos show inline in the Telegram chat, other files go as documents; email gets
the file as an attachment.

```bash
curl -F "message=Backup finished" http://relay:5000/notify
curl -F "message=Motion in the garden" -F "file=@snapshot.jpg" http://relay:5000/notify/telegram
curl -F "message=New scan" -F "file=@scan.pdf" -F "subject=New scan" http://relay:5000/notify/email
```

- FastAPI, one file, a few MB of RAM. Multi-arch image (`linux/arm64` for a Raspberry Pi, `linux/amd64`).
- Answers immediately; sending happens in the background, errors go to the log.
- Telegram: `sendPhoto` / `sendVideo` by file type, falls back to `sendDocument` if Telegram refuses.
- Email: STARTTLS, SSL or plain SMTP; optional custom subject and sender (same domain as `EMAIL_FROM`).
- JSON webhook endpoint for tools that speak `{"title", "message"}` (e.g. shoutrrr's generic service, Scrutiny).

## Endpoints

| Method | Path | Body | Sends to |
|---|---|---|---|
| POST | `/notify` | form: `message`, `file` (optional) | Telegram + email |
| POST | `/notify/telegram` | form: `message`, `file` (optional) | Telegram |
| POST | `/notify/email` | form: `message`, `file`, optional `subject`, `sender`, `sender_name` | email |
| POST | `/notify/json` | JSON `{"title": "...", "message": "..."}` | Telegram + email |
| GET | `/health` | | which channels are configured |

A channel whose variables are empty is skipped silently.

## Quick start

```bash
git clone https://github.com/aledv/notify-relay.git && cd notify-relay
cp .env.example .env   # fill in Telegram and/or SMTP
docker compose up -d
curl -F "message=It works" http://localhost:5000/notify
```

## Configuration (`.env`)

Names only, see `.env.example`.

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Bot token from @BotFather and the chat that receives the messages |
| `SMTP_SERVER`, `SMTP_PORT`, `SMTP_SECURITY` | SMTP server; `starttls` (default), `ssl` or `none` |
| `SMTP_USER`, `SMTP_PASS` | SMTP login (leave empty for an open relay) |
| `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_SUBJECT` | Sender, recipient, default subject |
| `LOG_FILE` | Also log to this file (stdout always) |

## Security

There is no authentication: run it on your LAN or a private Docker network, never exposed to
the internet. Anyone who can reach it can send you messages, but cannot read anything back.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v              # tests (no real Telegram/SMTP calls)
ruff check . && ruff format --check .                # Python linter
hadolint --config .hadolint.yaml Dockerfile          # Dockerfile linter
```

CI (`.github/workflows/image.yml`) runs the linters, then the tests, then publishes the image to
`ghcr.io/aledv/notify-relay` (`<short sha>` and `latest`).

## License

MIT, see [LICENSE](LICENSE).
