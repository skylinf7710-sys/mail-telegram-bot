import asyncio
import email
import html
import imaplib
import json
import os
from email.header import decode_header
from typing import Any

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application

CONFIG_FILE = "config.json"
DEFAULT_POLL_INTERVAL = 60
OWNER_CHAT_ID = 5127424995
poll_task = None

IMAP_BY_DOMAIN = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "mail.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "zoho.com": "imap.zoho.com",
    "aol.com": "imap.aol.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "yahoo.de": "imap.mail.yahoo.com",
    "yahoo.co.uk": "imap.mail.yahoo.com",
}


def is_owner(update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == OWNER_CHAT_ID


def guess_imap(email_value: str) -> str:
    if "@" not in email_value:
        return ""
    domain = email_value.split("@", 1)[1].lower().strip()
    return IMAP_BY_DOMAIN.get(domain, f"imap.{domain}")


def load_config() -> dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "emails" not in data:
                    data["emails"] = []
                if "chat_id" not in data:
                    data["chat_id"] = OWNER_CHAT_ID
                if "poll_interval" not in data:
                    data["poll_interval"] = DEFAULT_POLL_INTERVAL
                return data
        except Exception:
            pass
    return {
        "emails": [],
        "chat_id": OWNER_CHAT_ID,
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


def escape_html(text: str) -> str:
    return html.escape(text or "")


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


def build_start_text() -> str:
    return (
        "👋 <b>Привет! Я бот для пересылки почты в Telegram.</b>\n\n"
        "✅ <b>Уведомления будут приходить только вам.</b>\n"
        f"Ваш chat_id уже привязан: <code>{OWNER_CHAT_ID}</code>\n\n"
        "<b>Как начать:</b>\n"
        "1. Добавьте почту:\n"
        "<code>/add_email ЛИЧНАЯ mymail@gmail.com apppassword</code>\n\n"
        "2. Проверьте отправку:\n"
        "<code>/test</code>\n\n"
        "3. Запустите пересылку:\n"
        "<code>/run</code>\n\n"
        "<b>Важно:</b>\n"
        "• Для Gmail нужен <b>App Password</b>, а не обычный пароль\n"
        "• IMAP подставляется автоматически\n\n"
        "ℹ️ Все команды: <code>/help</code>"
    )


def build_help_text() -> str:
    return (
        "📚 <b>Команды бота</b>\n\n"
        "<b>Основные</b>\n"
        "/start — инструкция по запуску\n"
        "/help — список команд\n"
        "/test — тестовое сообщение\n\n"
        "<b>Почты</b>\n"
        "/add_email &lt;название&gt; &lt;email&gt; &lt;пароль&gt;\n"
        "Добавить почту с авто-IMAP\n"
        "Пример:\n"
        "<code>/add_email ЛИЧНАЯ mymail@gmail.com apppassword</code>\n\n"
        "/remove_email &lt;email&gt; — удалить почту\n"
        "/list_emails — показать все почты\n\n"
        "<b>Настройки</b>\n"
        "/set_poll &lt;секунды&gt; — интервал проверки\n"
        "/show_config — показать конфиг\n\n"
        "<b>Управление</b>\n"
        "/run — запустить пересылку\n"
        "/stop — остановить пересылку\n\n"
        "⚠️ Бот доступен только владельцу."
    )


def format_email_message(
    label: str,
    mailbox_email: str,
    from_header: str,
    subject: str,
    body: str,
) -> str:
    safe_label = escape_html(label or "БЕЗ НАЗВАНИЯ")
    safe_mailbox = escape_html(mailbox_email)
    safe_from = escape_html(from_header or "Неизвестно")
    safe_subject = escape_html(subject or "Без темы")

    clean_body = (body or "").strip()
    if len(clean_body) > 1400:
        clean_body = clean_body[:1400].rstrip() + "\n\n...[обрезано]"

    safe_body = escape_html(clean_body or "[пустое сообщение]")

    return (
        "📬 <b>НОВОЕ ПИСЬМО</b>\n"
        "══════════════════\n"
        f"📮 <b>Ящик:</b> <code>{safe_label}</code>\n"
        f"📧 <b>Адрес:</b> <code>{safe_mailbox}</code>\n"
        f"👤 <b>От:</b> {safe_from}\n"
        f"📝 <b>Тема:</b> {safe_subject}\n"
        "══════════════════\n"
        f"{safe_body}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    config = load_config()
    config["chat_id"] = OWNER_CHAT_ID
    save_config(config)

    await update.message.reply_text(build_start_text(), parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return
    await update.message.reply_text(build_help_text(), parse_mode="HTML")


async def add_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Использование:\n"
            "/add_email <название> <email> <password>\n\n"
            "Пример:\n"
            "/add_email ЛИЧНАЯ mymail@gmail.com apppassword"
        )
        return

    label = context.args[0].strip()
    email_value = context.args[1].strip()
    password = context.args[2].strip()
    imap_host = guess_imap(email_value)

    if not imap_host:
        await update.message.reply_text("Не удалось определить IMAP сервер.")
        return

    config = load_config()

    for item in config["emails"]:
        if item["email"].lower() == email_value.lower():
            await update.message.reply_text("Такая почта уже добавлена.")
            return

    config["emails"].append(
        {
            "label": label,
            "email": email_value,
            "password": password,
            "imap": imap_host,
            "seen_uids": [],
        }
    )
    config["chat_id"] = OWNER_CHAT_ID
    save_config(config)

    await update.message.reply_text(
        f"✅ Почта добавлена.\n"
        f"Название: {label}\n"
        f"Адрес: {email_value}\n"
        f"IMAP: {imap_host}"
    )


async def remove_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

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
    await update.message.reply_text(f"🗑 Почта {email_value} удалена.")


async def list_emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    config = load_config()
    emails = config.get("emails", [])

    if not emails:
        await update.message.reply_text("Почты ещё не добавлены.")
        return

    text = "📮 <b>Добавленные почты</b>\n\n"
    for idx, item in enumerate(emails, start=1):
        label = escape_html(item.get("label", "БЕЗ НАЗВАНИЯ"))
        addr = escape_html(mask_email(item["email"]))
        imap_host = escape_html(item["imap"])
        text += (
            f"{idx}. <b>{label}</b>\n"
            f"   📧 {addr}\n"
            f"   🌐 {imap_host}\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


async def set_poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

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

    await update.message.reply_text(f"⏱ Интервал установлен: {interval} сек.")


async def show_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    config = load_config()

    safe_emails = []
    for item in config.get("emails", []):
        safe_emails.append(
            {
                "label": item.get("label", ""),
                "email": item["email"],
                "password": "***",
                "imap": item["imap"],
            }
        )

    safe_config = {
        "emails": safe_emails,
        "chat_id": OWNER_CHAT_ID,
        "poll_interval": config.get("poll_interval", DEFAULT_POLL_INTERVAL),
    }

    await update.message.reply_text(
        "Текущая конфигурация:\n" +
        json.dumps(safe_config, indent=2, ensure_ascii=False)
    )


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    text = (
        "✅ <b>Тестовое сообщение</b>\n"
        "══════════════════\n"
        "Бот умеет отправлять сообщения в ваш чат.\n\n"
        "Теперь можно использовать /run"
    )

    try:
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=text,
            parse_mode="HTML",
        )
        await update.message.reply_text("Тест отправлен.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка теста: {e}")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task

    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    config = load_config()

    if not config.get("emails"):
        await update.message.reply_text("Сначала добавь хотя бы одну почту через /add_email")
        return

    if poll_task and not poll_task.done():
        await update.message.reply_text("Пересылка уже запущена.")
        return

    poll_task = context.application.create_task(poll_mail_loop(context))
    await update.message.reply_text("🚀 Запускаю пересылку писем...")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global poll_task

    if not is_owner(update):
        await update.message.reply_text("⛔ Этот бот доступен только @skylinf.")
        return

    if poll_task and not poll_task.done():
        poll_task.cancel()
        await update.message.reply_text("⛔ Пересылка остановлена.")
    else:
        await update.message.reply_text("Пересылка не была запущена.")


async def poll_mail_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    while True:
        config = load_config()
        chat_id = OWNER_CHAT_ID
        interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        emails = config.get("emails", [])

        for mailbox in emails:
            try:
                label = mailbox.get("label", "БЕЗ НАЗВАНИЯ")
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

                        text = format_email_message(
                            label=label,
                            mailbox_email=email_addr,
                            from_header=from_header,
                            subject=subject,
                            body=body,
                        )

                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=text[:4096],
                                parse_mode="HTML",
                            )
                        except Exception:
                            fallback_text = (
                                f"НОВОЕ ПИСЬМО\n"
                                f"Ящик: {label}\n"
                                f"Адрес: {email_addr}\n"
                                f"От: {from_header}\n"
                                f"Тема: {subject}\n\n"
                                f"{body or '[пустое сообщение]'}"
                            )
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=fallback_text[:4096],
                            )

                        seen_uids.add(uid)
                        changed = True

                    if changed:
                        mailbox["seen_uids"] = list(seen_uids)[-300:]

            except imaplib.IMAP4.error as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка IMAP для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка для {mailbox.get('label', mailbox.get('email', 'unknown'))}: {e}",
                )

        save_config(config)
        await asyncio.sleep(interval)


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    application: Application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add_email", add_email_cmd))
    application.add_handler(CommandHandler("remove_email", remove_email_cmd))
    application.add_handler(CommandHandler("list_emails", list_emails_cmd))
    application.add_handler(CommandHandler("set_poll", set_poll_cmd))
    application.add_handler(CommandHandler("show_config", show_config_cmd))
    application.add_handler(CommandHandler("test", test_cmd))
    application.add_handler(CommandHandler("run", run_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))

    application.run_polling()


if __name__ == "__main__":
    main()
