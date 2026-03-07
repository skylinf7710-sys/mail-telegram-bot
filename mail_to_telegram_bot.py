"""Mail to Telegram Bot

This script connects to an IMAP email account and forwards new (unseen) messages
to a specified Telegram chat. It reads configuration from environment
variables so that it can be deployed easily on a hosting platform such as
JustRunMy.App or fps.ms. The script continuously polls the IMAP server at
regular intervals and sends a formatted summary of each new email via the
Telegram Bot API.

Environment variables used:

* **IMAP_HOST** – The hostname of your IMAP server (e.g. `imap.gmail.com`).
* **IMAP_PORT** – The port number for IMAP over SSL. Defaults to `993` if
  unspecified.
* **EMAIL_USER** – The username (usually the email address) for the email
  account.
* **EMAIL_PASSWORD** – The password (or app-specific password) for the
  email account.
* **TELEGRAM_TOKEN** – The token for your Telegram bot obtained from
  BotFather.
* **TELEGRAM_CHAT_ID** – The chat ID (user, group, or channel) where you
  want to forward emails.
* **POLL_INTERVAL** – Time in seconds between checks for new mail. Defaults
  to 60 seconds.
* **FOLDER** – The mailbox folder to monitor (e.g. `INBOX`). Defaults to
  `INBOX`.

To deploy this script:

1. Set up a Telegram bot with BotFather and obtain the token. Add the bot
   to the chat or channel where you want notifications and obtain the chat
   ID (you can use bots like `@myidbot` to fetch chat IDs).
2. Ensure IMAP access is enabled for your email account and note your
   server details.
3. On your hosting platform, configure the environment variables listed
   above with your credentials.
4. Run the script in a persistent environment. It will continuously poll
   your mailbox and forward new messages as they arrive.

Note: This script sends only a snippet of each email body to avoid very
long messages and does not handle attachments. You can adjust the
snippet length or extend it to handle attachments as needed.
"""

import email
import imaplib
import os
import time
from typing import Optional

import requests


def send_to_telegram(token: str, chat_id: str, message: str) -> Optional[int]:
    """Send a message to a Telegram chat using the Bot API.

    Args:
        token: The bot token.
        chat_id: The target chat ID.
        message: The message text to send (HTML is allowed).

    Returns:
        The HTTP status code returned by Telegram or None on error.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        # Disable link previews to keep messages concise; remove this line
        # if you want previews for URLs included in email bodies.
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, data=payload, timeout=15)
        response.raise_for_status()
        return response.status_code
    except Exception as exc:
        # Print the error; in production you might want to log this instead.
        print(f"Failed to send message to Telegram: {exc}")
        return None


def extract_plain_text(msg: email.message.Message) -> str:
    """Extract a plain text body from an email message.

    If the message is multipart, it tries to find the first part that is
    `text/plain` and not an attachment. If no such part exists, it falls
    back to the raw payload.

    Args:
        msg: The email message object.

    Returns:
        A string containing the email body in plain text.
    """
    if msg.is_multipart():
        # Walk through the message parts to find a text/plain section
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
                except Exception:
                    continue
    # Fallback: decode the main payload
    try:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""
    except Exception:
        return ""


def format_email_for_telegram(msg: email.message.Message) -> str:
    """Format an email message into a string suitable for Telegram.

    The formatted message includes the From, Subject, Date headers and a
    snippet of the body. HTML tags are used for simple formatting.

    Args:
        msg: The email message to format.

    Returns:
        A formatted string.
    """
    from_addr = msg.get("From", "")
    subject = msg.get("Subject", "(No Subject)")
    date = msg.get("Date", "")
    body_text = extract_plain_text(msg)
    # Limit the body snippet length to avoid sending extremely long messages.
    max_chars = 1000
    snippet = body_text[:max_chars].strip()
    # Escape HTML special characters in the snippet. Telegram supports a
    # subset of HTML; by replacing angle brackets we prevent injection.
    snippet = (
        snippet.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    formatted = (
        f"<b>From:</b> {from_addr}\n"
        f"<b>Subject:</b> {subject}\n"
        f"<b>Date:</b> {date}\n\n"
        f"{snippet}"
    )
    return formatted


def fetch_and_forward(
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    folder: str,
    telegram_token: str,
    telegram_chat_id: str,
) -> None:
    """Connect to an IMAP server and forward unseen emails to Telegram.

    This function opens an IMAP4 SSL connection, logs in, selects the
    specified folder, searches for unseen messages, and forwards each one
    using the Telegram bot API. After processing, the connection is closed.

    Args:
        imap_host: IMAP server hostname.
        imap_port: IMAP server port (SSL).
        username: Email account username.
        password: Email account password.
        folder: Mailbox folder to monitor.
        telegram_token: Telegram bot token.
        telegram_chat_id: ID of the chat to forward messages to.
    """
    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        mail.login(username, password)
        # Select the folder; readonly=False allows setting flags but we don't
        # need to modify messages. Use readonly mode to avoid accidental
        # changes.
        mail.select(folder, readonly=True)
        # Search for unseen messages
        typ, msgnums = mail.search(None, "UNSEEN")
        if typ != "OK":
            print(f"IMAP search failed: {typ}")
            return
        for num in msgnums[0].split():
            typ, data = mail.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            raw_email = data[0][1]
            try:
                message = email.message_from_bytes(raw_email)
            except Exception as exc:
                print(f"Failed to parse email: {exc}")
                continue
            formatted = format_email_for_telegram(message)
            send_to_telegram(telegram_token, telegram_chat_id, formatted)
        mail.close()
        mail.logout()
    except Exception as exc:
        print(f"IMAP error: {exc}")


def main() -> None:
    """Main loop: repeatedly check for new emails and forward them."""
    imap_host = os.environ.get("IMAP_HOST")
    imap_port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")
    telegram_token = os.environ.get("TELEGRAM_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    poll_interval = int(os.environ.get("POLL_INTERVAL", "60"))
    folder = os.environ.get("FOLDER", "INBOX")

    if not all([imap_host, username, password, telegram_token, telegram_chat_id]):
        missing = [
            name
            for name, value in [
                ("IMAP_HOST", imap_host),
                ("EMAIL_USER", username),
                ("EMAIL_PASSWORD", password),
                ("TELEGRAM_TOKEN", telegram_token),
                ("TELEGRAM_CHAT_ID", telegram_chat_id),
            ]
            if not value
        ]
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    print(
        f"Starting mail forwarding: IMAP_HOST={imap_host}, folder={folder}, "
        f"poll interval={poll_interval}s"
    )
    while True:
        fetch_and_forward(
            imap_host,
            imap_port,
            username,
            password,
            folder,
            telegram_token,
            telegram_chat_id,
        )
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
