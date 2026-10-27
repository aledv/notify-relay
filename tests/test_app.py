"""Unit tests: endpoints schedule the right channels, Telegram/email senders build the right
requests (no real Telegram or SMTP calls). Run: python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as relay  # noqa: E402


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg):
        self.sent = msg


CONFIG = {
    "TELEGRAM_TOKEN": "123:abc",
    "TELEGRAM_CHAT_ID": "42",
    "SMTP_SERVER": "smtp.example.com",
    "SMTP_USER": "user",
    "SMTP_PASS": "pw",
    "EMAIL_FROM": "alerts@example.com",
    "EMAIL_TO": "me@example.com",
}


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tg = mock.patch.object(relay, "send_telegram").start()
        self.mail = mock.patch.object(relay, "send_email").start()
        for k, v in CONFIG.items():
            mock.patch.object(relay, k, v).start()
        self.client = TestClient(relay.app)

    def tearDown(self):
        mock.patch.stopall()

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "telegram": True, "email": True})

    def test_notify_sends_both(self):
        r = self.client.post("/notify", data={"message": "hello"})
        self.assertEqual(r.json()["sent_to"], ["telegram", "email"])
        self.tg.assert_called_once_with("hello", None, None)
        self.mail.assert_called_once_with("hello", None, None)

    def test_notify_with_file(self):
        r = self.client.post("/notify", data={"message": "log"}, files={"file": ("a.txt", b"line")})
        self.assertEqual(r.json()["file"], "a.txt")
        self.tg.assert_called_once_with("log", "a.txt", b"line")

    def test_telegram_only(self):
        self.client.post("/notify/telegram", data={"message": "t"})
        self.tg.assert_called_once()
        self.mail.assert_not_called()

    def test_email_only_with_custom_sender(self):
        r = self.client.post(
            "/notify/email",
            data={"message": "x", "subject": "New scan", "sender": "scanner@example.com", "sender_name": "Scanner"},
        )
        self.assertEqual(r.status_code, 200)
        self.tg.assert_not_called()
        self.mail.assert_called_once_with("x", None, None, "New scan", "scanner@example.com", "Scanner")

    def test_email_sender_outside_domain_rejected(self):
        r = self.client.post("/notify/email", data={"message": "x", "sender": "ceo@other.org"})
        self.assertEqual(r.status_code, 400)
        self.mail.assert_not_called()

    def test_json_webhook_joins_title(self):
        self.client.post("/notify/json", json={"title": "Disk", "message": "failing"})
        self.tg.assert_called_once_with("Disk\n\nfailing")

    def test_missing_message_rejected(self):
        self.assertEqual(self.client.post("/notify", data={}).status_code, 422)


class SenderTest(unittest.TestCase):
    def setUp(self):
        for k, v in CONFIG.items():
            mock.patch.object(relay, k, v).start()
        FakeSMTP.instances = []

    def tearDown(self):
        mock.patch.stopall()

    def telegram_calls(self, filename, statuses=(200,)):
        responses = [mock.Mock(ok=code < 300, status_code=code) for code in statuses]
        with mock.patch.object(relay.requests, "post", side_effect=responses) as post:
            relay.send_telegram("msg", filename, b"data")
        return [(c.args[0].rsplit("/", 1)[1], list(c.kwargs["files"])) for c in post.call_args_list]

    def test_telegram_photo_video_document(self):
        self.assertEqual(self.telegram_calls("snap.jpg"), [("sendPhoto", ["photo"])])
        self.assertEqual(self.telegram_calls("clip.mp4"), [("sendVideo", ["video"])])
        self.assertEqual(self.telegram_calls("log.txt"), [("sendDocument", ["document"])])

    def test_telegram_media_falls_back_to_document(self):
        calls = self.telegram_calls("big.png", statuses=(400, 200))
        self.assertEqual(calls, [("sendPhoto", ["photo"]), ("sendDocument", ["document"])])

    def test_telegram_never_raises_and_hides_token(self):
        with mock.patch.object(relay.requests, "post", side_effect=RuntimeError("https://api.telegram.org/bot123:abc")):
            with self.assertLogs("notify-relay", "ERROR") as logs:
                relay.send_telegram("x")
        self.assertNotIn("123:abc", "".join(logs.output))

    def test_telegram_disabled_without_token(self):
        with mock.patch.object(relay, "TELEGRAM_TOKEN", ""), mock.patch.object(relay.requests, "post") as post:
            relay.send_telegram("x")
        post.assert_not_called()

    def test_email_starttls_with_attachment(self):
        with mock.patch.object(relay.smtplib, "SMTP", FakeSMTP):
            relay.send_email(
                "scan", "scan.pdf", b"%PDF-1.4", subject="New scan", sender="scanner@example.com", sender_name="Scanner"
            )
        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.calls, ["starttls", "login"])
        self.assertEqual(smtp.sent["From"], "Scanner <scanner@example.com>")
        self.assertEqual(smtp.sent["Subject"], "New scan")
        att = next(smtp.sent.iter_attachments())
        self.assertEqual(att.get_content_type(), "application/pdf")

    def test_email_ssl(self):
        with mock.patch.object(relay, "SMTP_SECURITY", "ssl"), mock.patch.object(relay.smtplib, "SMTP_SSL", FakeSMTP):
            relay.send_email("x")
        self.assertEqual(FakeSMTP.instances[0].calls, ["login"])
        self.assertEqual(FakeSMTP.instances[0].sent["Subject"], relay.EMAIL_SUBJECT)

    def test_email_never_raises(self):
        with mock.patch.object(relay.smtplib, "SMTP", side_effect=OSError("down")):
            relay.send_email("x")


if __name__ == "__main__":
    unittest.main()
