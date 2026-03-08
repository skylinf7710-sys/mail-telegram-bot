"""
Interactive Mail-to-Telegram bot.

This script provides a Telegram bot that can be configured entirely through
chat commands. It allows the user to set their IMAP credentials, the
destination chat ID and other options without editing the code or using
environment variables. Once configured, the bot monitors the specified
mailbox for new, unread messages and forwards them to the chosen
Telegram chat. Configuration details are stored in a local JSON file
(``config.json``) so that they persist across restarts.

Commands:
    /start            – Show a welcome message and basic usage.
    /set_email        – Set the email address used for IMAP login.
    /set_password     – Set the password (e.g. app password) for IMAP login.
    /set_imap         – Set the IMAP server hostname (e.g. imap.gmail.com).
    /set_chat_id      – Set the Telegram chat ID where messages should be forwarded.
    /set_poll         – Set the polling interval in seconds (optional).
    /show_config      – Display the current configuration (hiding the password).
    /run              – Start the mail polling loop.
    /stop             – Stop the mail polling loop.

To use this bot you need a Telegram bot token from BotFather.  The
token should be supplied via the ``TELEGRAM_TOKEN`` environment variable.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import json
import os
from email.header import decode_header
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application


CONFIG_FILE = "config.json"
DEFAULT_POLL_INTERVAL = 60  # seconds


def load_config() -> Dict[str, Any]:
    """Load configuration from CONFIG_FILE.  Returns an empty dict if
    the file doesn't exist or cannot be parsed."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config: Dict[str, Any]) -> None:
    """Persist configuration to CONFIG_FILE."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message and basic instructions."""
    text = (
        "Привет! Я бот для пересылки почты в Telegram.\n\n"
        "Используйте следующие команды для настройки:\n"
        "/set_email <адрес> – установить email\n"
        "/set_password <пароль> – установить пароль (app‑пароль)\n"
        "/set_imap <сервер> – установить IMAP‑сервер (например, imap.gmail.com)\n"
        "/set_chat_id <id> – установить chat_id для пересылки\n"
        "/set_poll <секунды> – интервал проверки (>=30, по умолчанию 60)\n"
        "/show_config – показать текущую конфигурацию\n"
        "/run – запустить пересылку\n"
        "/stop – остановить пересылку"
    )
    await update.message.reply_text(text)


async def set_config_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, value: str) -> None:
    config = load_config()
    config[key] = value
    save_config(config)
    await update.message.reply_text(f"Параметр '{key}' установлен.")


async def set_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /set_email <адрес>")
        return
    await set_config_value(update, context, "email", context.args[0])


async def set_password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /set_password <пароль>")
        return
    await set_config_value(update, context, "password", context.args[0])


async def set_imap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /set_imap <imap-сервер>")
        return
    await set_config_value(update, context, "imap", context.args[0])


async def set_chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /set_chat_id <chat_id>")
        return
    await set_config_value(update, context, "chat_id", context.args[0])


async def set_poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /set_poll <секунды> (рекомендуется >=30)")
        return
    try:
        interval = int(context.args[0])
        if interval < 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом >= 10 секунд.")
        return
    await set_config_value(update, context, "poll_interval", interval)


async def show_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    if not config:
        await update.message.reply_text("Конфигурация не задана.")
        return
    safe = {k: ("***" if k == "password" else v) for k, v in config.items()}
    await update.message.reply_text(
        "Текущая конфигурация:\n" + json.dumps(safe, indent=2, ensure_ascii=False)
    )


poll_task: Optional[asyncio.Task] = None


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task
    config = load_config()
    required = ["email", "password", "imap", "chat_id"]
    missing = [k for k in required if k not in config]
    if missing:
        await update.message.reply_text(
            "Не хватает параметров: " + ", ".join(missing)
        )
        return
    if poll_task and not poll_task.done():
        await update.message.reply_text("Пересылка уже запущена.")
        return
    await update.message.reply_text("Запускаю пересылку писем...")
    poll_task = context.application.create_task(poll_mail_loop(context, config))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task
    if poll_task and not poll_task.done():
        poll_task.cancel()
        await update.message.reply_text("Пересылка остановлена.")
    else:
        await update.message.reply_text("Пересылка не была запущена.")


async def poll_mail_loop(context: ContextTypes.DEFAULT_TYPE, config: Dict[str, Any]) -> None:
    email_addr = config["email"]
    password = config["password"]
    imap_host = config["imap"]
    chat_id = config["chat_id"]
    interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
    seen_uids: set[str] = set()
    while True:
        try:
            with imaplib.IMAP4_SSL(imap_host) as imap:
                imap.login(email_addr, password)
                imap.select("INBOX")
                status, data = imap.search(None, "UNSEEN")
                if status == "OK":
                    for num in data[0].split():
                        uid = num.decode()
                        if uid in seen_uids:
                            continue
                        seen_uids.add(uid)
                        status, msg_data = imap.fetch(num, "(RFC822)")
                        if status != "OK" or not msg_data:
                            continue
                        raw_email = msg_data[0][1]
                        message = email.message_from_bytes(raw_email)
                        subject = decode_header(message.get("Subject", ""))[0][0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(errors="ignore")
                        from_header = message.get("From", "")
                        body = extract_plain_text(message)
                        text = f"<b>From:</b> {from_header}\n<b>Subject:</b> {subject}\n\n{body}"
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id, text=text[:4096], parse_mode="HTML"
                            )
                        except Exception:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=(f"From: {from_header}\nSubject: {subject}\n\n{body}")[:4096]
                            )
        except imaplib.IMAP4.error as e:
            await context.application.bot.send_message(
                chat_id=chat_id,
                text=f"Ошибка IMAP: {e}. Повтор через {interval} сек."
            )
        except Exception as e:
            await context.application.bot.send_message(
                chat_id=chat_id,
                text=f"Ошибка: {e}. Повтор через {interval} сек."
            )
        await asyncio.sleep(interval)


def extract_plain_text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                try:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                return payload.decode(message.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                pass
    return ""


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_TOKEN environment variable is required to run this bot."
        )
    application: Application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_email", set_email_cmd))
    application.add_handler(CommandHandler("set_password", set_password_cmd))
    application.add_handler(CommandHandler("set_imap", set_imap_cmd))
    application.add_handler(CommandHandler("set_chat_id", set_chat_id_cmd))
    application.add_handler(CommandHandler("set_poll", set_poll_cmd))
    application.add_handler(CommandHandler("show_config", show_config_cmd))
    application.add_handler(CommandHandler("run", run_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.run_polling()


if __name__ == "__main__":
    main()