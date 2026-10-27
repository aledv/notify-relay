"""notify-relay: a tiny HTTP service that forwards notifications to Telegram and/or email.

Your scripts and services POST a message (and optionally a file) to one endpoint; the relay
holds the credentials and sends it on. Photos and videos show inline in Telegram, anything
else goes as a document; email gets the file as an attachment.
"""

import logging
import mimetypes
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SMTP_SERVER = os.getenv("SMTP_SERVER", "")
SMTP_PORT = int(os.getenv("SMTP_PORT") or "587")
SMTP_SECURITY = (os.getenv("SMTP_SECURITY") or "starttls").lower()  # starttls | ssl | none
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT") or "Notification"
LOG_FILE = os.getenv("LOG_FILE", "")

handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    handlers.append(logging.FileHandler(LOG_FILE))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
log = logging.getLogger("notify-relay")

app = FastAPI(title="notify-relay")


def telegram_enabled() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def email_enabled() -> bool:
    return bool(SMTP_SERVER and EMAIL_FROM and EMAIL_TO)


async def _read(file: UploadFile | None) -> tuple[str | None, bytes | None]:
    if not file:
        return None, None
    return file.filename, await file.read()


@app.get("/health")
def health():
    return {"status": "ok", "telegram": telegram_enabled(), "email": email_enabled()}


@app.post("/notify")
async def notify(background_tasks: BackgroundTasks, message: str = Form(...), file: UploadFile = File(None)):
    """Send to every configured channel."""
    name, content = await _read(file)
    log.info(f"/notify: {message!r} (file: {name})")
    background_tasks.add_task(send_telegram, message, name, content)
    background_tasks.add_task(send_email, message, name, content)
    return {"status": "ok", "sent_to": ["telegram", "email"], "message": message, "file": name}


@app.post("/notify/telegram")
async def notify_telegram(background_tasks: BackgroundTasks, message: str = Form(...), file: UploadFile = File(None)):
    name, content = await _read(file)
    log.info(f"/notify/telegram: {message!r} (file: {name})")
    background_tasks.add_task(send_telegram, message, name, content)
    return {"status": "ok", "sent_to": "telegram", "message": message, "file": name}


def sender_allowed(sender: str) -> bool:
    """A custom sender address is only accepted in the same domain as EMAIL_FROM."""
    domain = EMAIL_FROM.rsplit("@", 1)[-1].lower() if "@" in EMAIL_FROM else ""
    return bool(domain) and sender.count("@") == 1 and sender.rsplit("@", 1)[1].lower() == domain


@app.post("/notify/email")
async def notify_email(
    background_tasks: BackgroundTasks,
    message: str = Form(...),
    file: UploadFile = File(None),
    subject: str = Form(None),
    sender: str = Form(None),
    sender_name: str = Form(None),
):
    if sender and not sender_allowed(sender):
        return JSONResponse(
            status_code=400, content={"status": "error", "detail": "sender outside the EMAIL_FROM domain"}
        )
    name, content = await _read(file)
    log.info(f"/notify/email: {message!r} (file: {name})")
    background_tasks.add_task(send_email, message, name, content, subject, sender, sender_name)
    return {"status": "ok", "sent_to": "email", "message": message, "file": name}


class JsonNotification(BaseModel):
    title: str = ""
    message: str


@app.post("/notify/json")
async def notify_json(background_tasks: BackgroundTasks, notification: JsonNotification):
    """JSON webhook ({"title": "...", "message": "..."}), e.g. shoutrrr's generic service in Scrutiny."""
    message = f"{notification.title}\n\n{notification.message}" if notification.title else notification.message
    log.info(f"/notify/json: {notification.title!r}")
    background_tasks.add_task(send_telegram, message)
    background_tasks.add_task(send_email, message)
    return {"status": "ok", "sent_to": ["telegram", "email"], "message": message}


# Telegram method + form field by file type: photos and videos show inline in the chat,
# everything else is sent as a document.
_TELEGRAM_MEDIA = {"image": ("sendPhoto", "photo"), "video": ("sendVideo", "video")}


def _telegram_post(method: str, data: dict, files: dict | None = None):
    return requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", data=data, files=files, timeout=60)


def send_telegram(message: str, filename: str | None = None, content: bytes | None = None):
    """Never raises: errors are logged (without the token, which is part of the URL)."""
    if not telegram_enabled():
        return
    try:
        if filename and content:
            kind = (mimetypes.guess_type(filename)[0] or "").split("/")[0]
            method, field = _TELEGRAM_MEDIA.get(kind, ("sendDocument", "document"))
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": message[:1024]}
            if method == "sendVideo":
                data["supports_streaming"] = "true"
            response = _telegram_post(method, data, {field: (filename, content)})
            if method != "sendDocument" and not response.ok:
                # e.g. a photo too large or with odd dimensions: send it as a plain file instead
                log.warning(f"Telegram {method} refused (HTTP {response.status_code}), sending as a document")
                response = _telegram_post("sendDocument", data, {"document": (filename, content)})
        else:
            response = _telegram_post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096]})
        response.raise_for_status()
        log.info(f"Telegram: sent (file: {filename})")
    except Exception as e:
        log.error(f"Telegram: sending failed ({type(e).__name__})")


def send_email(
    message: str,
    filename: str | None = None,
    content: bytes | None = None,
    subject: str | None = None,
    sender: str | None = None,
    sender_name: str | None = None,
):
    """Never raises: errors are logged."""
    if not email_enabled():
        return
    try:
        msg = EmailMessage()
        from_addr = sender or EMAIL_FROM
        msg["From"] = formataddr((sender_name, from_addr)) if sender_name else from_addr
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject or EMAIL_SUBJECT
        msg.set_content(message)
        if filename and content:
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

        smtp_class = smtplib.SMTP_SSL if SMTP_SECURITY == "ssl" else smtplib.SMTP
        with smtp_class(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            if SMTP_SECURITY == "starttls":
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log.info(f"Email: sent to {EMAIL_TO} (subject: {msg['Subject']!r}, file: {filename})")
    except Exception as e:
        log.error(f"Email: sending failed ({type(e).__name__}: {e})")
