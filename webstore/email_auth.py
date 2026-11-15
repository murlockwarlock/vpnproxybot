import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage


def _send(recipient, subject, text):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_APP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "")
    if not username or not password or not sender:
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)


async def send_email(recipient, subject, text):
    await asyncio.to_thread(_send, recipient, subject, text)
