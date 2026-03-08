import asyncio
import email
import imaplib
import json
import os
from email.header import decode_header
from typing import Any

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application

CONFIG_FILE = "config.json"
DEFAULT_POLL_INTERVAL = 60
poll_task = None


def load_config() -> dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "emails" not in data:
                    data["emails"] = []
                return data
        except Exception:
            pass
    return {
        "emails": [],
        "chat_id": "",
        "poll_interval": DEFAULT_POLL_INTERVAL,
    }


def save_config(config: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def mask_email(email_value: str) -> str:
    if "@" not in email_value:
        return email_value
    name, domain = email_value.split("@", 1)
    if len(name) <= 2:
        return "*" * len(name) + "@" + domain
    return name[:2] + "*" * (len(name) - 2) + "@" + domain


def extract_plain_text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    try:
                        return payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="ignore",
                        ).strip()
                    except Exception:
                        continue
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                return payload.decode(
                    message.get_content_charset() or "utf-8",
                    errors="ignore",
                ).strip()
            except Exception:
                pass
    return ""


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                result.append(part.decode("utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я бот для пересылки почты в Telegram.\n\n"
        "Команды:\n"
        "/add_email <email> <app_password> <imap>\n"
        "/remove_email <email>\n"
        "/list_emails\n"
        "/set_chat_id <chat_id>\n"
        "/set_poll <секунды>\n"
        "/show_config\n"
        "/run\n"
        "/stop\n\n"
        "Пример:\n"
        "/add_email mymail@gmail.com abcd1234 imap.gmail.com"
    )
    await update.message.reply_text(text)


async def add_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text(
            "Использование:\n/add_email <email> <password> <imap>\n\n"
            "Пример:\n/add_email mymail@gmail.com apppassword imap.gmail.com"
        )
        return

    email_value = context.args[0].strip()
    password = context.args[1].strip()
    imap_host = context.args[2].strip()

    config = load_config()

    for item in config["emails"]:
        if item["email"].lower() == email_value.lower():
            await update.message.reply_text("Такая почта уже добавлена.")
            return

    config["emails"].append(
        {
            "email": email_value,
            "password": password,
            "imap": imap_host,
            "seen_uids": [],
        }
    )
    save_config(config)

    await update.message.reply_text(f"Почта {email_value} добавлена.")


async def remove_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование:\n/remove_email <email>")
        return

    email_value = context.args[0].strip().lower()
    config = load_config()

    old_len = len(config["emails"])
    config["emails"] = [
        item for item in config["emails"]
        if item["email"].lower() != email_value
    ]

    if len(config["emails"]) == old_len:
        await update.message.reply_text("Такая почта не найдена.")
        return

    save_config(config)
    await update.message.reply_text(f"Почта {email_value} удалена.")


async def list_emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    emails = config.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.")
        return

    text = "Добавленные почты:\n\n"
    for idx, item in enumerate(emails, start=1):
        text += f"{idx}. {mask_email(item['email'])} | {item['imap']}\n"

    await update.message.reply_text(text)


async def set_chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование:\n/set_chat_id <chat_id>")
        return

    chat_id = context.args[0].strip()
    config = load_config()
    config["chat_id"] = chat_id
    save_config(config)

    await update.message.reply_text(f"Параметр 'chat_id' установлен: {chat_id}")


async def set_poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование:\n/set_poll <секунды>")
        return

    try:
        interval = int(context.args[0])
        if interval < 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Интервал должен быть числом не меньше 10.")
        return

    config = load_config()
    config["poll_interval"] = interval
    save_config(config)

    await update.message.reply_text(f"Интервал установлен: {interval} сек.")


async def show_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()

    safe_emails = []
    for item in config.get("emails", []):
        safe_emails.append(
            {
                "email": item["email"],
                "password": "***",
                "imap": item["imap"],
            }
        )

    safe_config = {
        "emails": safe_emails,
        "chat_id": config.get("chat_id", ""),
        "poll_interval": config.get("poll_interval", DEFAULT_POLL_INTERVAL),
    }

    await update.message.reply_text(
        "Текущая конфигурация:\n" +
        json.dumps(safe_config, indent=2, ensure_ascii=False)
    )


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task

    config = load_config()

    if not config.get("chat_id"):
        await update.message.reply_text("Сначала установи chat_id через /set_chat_id")
        return

    if not config.get("emails"):
        await update.message.reply_text("Сначала добавь хотя бы одну почту через /add_email")
        return

    if poll_task and not poll_task.done():
        await update.message.reply_text("Пересылка уже запущена.")
        return

    poll_task = context.application.create_task(poll_mail_loop(context))
    await update.message.reply_text("Запускаю пересылку писем...")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task

    if poll_task and not poll_task.done():
        poll_task.cancel()
        await update.message.reply_text("Пересылка остановлена.")
    else:
        await update.message.reply_text("Пересылка не была запущена.")


async def poll_mail_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    while True:
        config = load_config()
        chat_id = config.get("chat_id", "")
        interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        emails = config.get("emails", [])

        for mailbox in emails:
            try:
                email_addr = mailbox["email"]
                password = mailbox["password"]
                imap_host = mailbox["imap"]
                seen_uids = set(mailbox.get("seen_uids", []))

                with imaplib.IMAP4_SSL(imap_host) as imap:
                    imap.login(email_addr, password)
                    imap.select("INBOX")

                    status, data = imap.search(None, "UNSEEN")
                    if status != "OK":
                        continue

                    changed = False

                    for num in data[0].split():
                        uid = num.decode()

                        if uid in seen_uids:
                            continue

                        status, msg_data = imap.fetch(num, "(RFC822)")
                        if status != "OK" or not msg_data:
                            continue

                        raw_email = msg_data[0][1]
                        message = email.message_from_bytes(raw_email)

                        subject = decode_mime_header(message.get("Subject", "Без темы"))
                        from_header = decode_mime_header(message.get("From", "Неизвестно"))
                        body = extract_plain_text(message)

                        if len(body) > 1500:
                            body = body[:1500] + "\n\n...[обрезано]"

                        text = (
                            f"📧 <b>{email_addr}</b>\n"
                            f"<b>From:</b> {from_header}\n"
                            f"<b>Subject:</b> {subject}\n\n"
                            f"{body or '[пустое сообщение]'}"
                        )

                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=text[:4096],
                                parse_mode="HTML",
                            )
                        except Exception:
                            fallback_text = (
                                f"📧 {email_addr}\n"
                                f"From: {from_header}\n"
                                f"Subject: {subject}\n\n"
                                f"{body or '[пустое сообщение]'}"
                            )
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=fallback_text[:4096],
                            )

                        seen_uids.add(uid)
                        changed = True

                    if changed:
                        mailbox["seen_uids"] = list(seen_uids)

            except imaplib.IMAP4.error as e:
                if chat_id:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Ошибка IMAP для {mailbox.get('email', 'unknown')}: {e}",
                    )
            except Exception as e:
                if chat_id:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Ошибка для {mailbox.get('email', 'unknown')}: {e}",
                    )

        save_config(config)
        await asyncio.sleep(interval)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    application: Application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_email", add_email_cmd))
    application.add_handler(CommandHandler("remove_email", remove_email_cmd))
    application.add_handler(CommandHandler("list_emails", list_emails_cmd))
    application.add_handler(CommandHandler("set_chat_id", set_chat_id_cmd))
    application.add_handler(CommandHandler("set_poll", set_poll_cmd))
    application.add_handler(CommandHandler("show_config", show_config_cmd))
    application.add_handler(CommandHandler("run", run_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))

    application.run_polling()


if __name__ == "__main__":
    main()
