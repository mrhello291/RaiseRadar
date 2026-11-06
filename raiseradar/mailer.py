"""Sending over SMTP (Gmail with an app password by default). Credentials come from the environment / .env only."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


class MailError(Exception):
    pass


def build_message(sender_name: str, sender: str, reply_to: str, to: str, subject: str, body: str) -> EmailMessage:
    for v in (sender_name, sender, reply_to, to, subject):   # header injection guard
        if "\n" in v or "\r" in v:
            raise MailError("line break in an email header field")
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to
    msg["Reply-To"] = reply_to or sender
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(body)
    return msg


def send(host: str, port: int, user: str, password: str, msg: EmailMessage, smtp_factory=None) -> str:
    """Send one message; returns its Message-ID. Raises MailError with a readable reason."""
    if not user or not password:
        raise MailError("SMTP login missing: set OUTREACH_SMTP_USER and OUTREACH_SMTP_PASSWORD in .env (a Gmail app password)")
    try:
        if smtp_factory:
            s = smtp_factory(host, port)
        elif port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls(context=ssl.create_default_context())
        with s:
            s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("the mail server rejected the login (for Gmail use an app password, not your normal password)") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from None
    return msg["Message-ID"]


def check_login(host: str, port: int, user: str, password: str, smtp_factory=None) -> None:
    """Log in without sending anything."""
    if not user or not password:
        raise MailError("OUTREACH_SMTP_USER / OUTREACH_SMTP_PASSWORD are not set")
    try:
        s = smtp_factory(host, port) if smtp_factory else (smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30))
        with s:
            if not smtp_factory and port != 465:
                s.starttls()
            s.login(user, password)
    except smtplib.SMTPAuthenticationError:
        raise MailError("the mail server rejected the login (for Gmail use an app password)") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from None
