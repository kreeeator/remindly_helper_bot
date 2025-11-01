# bot.py — свободные команды + меню + инлайн-кнопки + устойчивые ответы/обработчик ошибок
import os
import re
import sqlite3
import time
import asyncio
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz
from dateparser.search import search_dates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, RetryAfter, NetworkError

# ---------- Константы/тексты ----------
DB_PATH = os.getenv("DB_PATH", "reminders.db")
DEFAULT_TZ = "UTC"

BTN_CREATE = "➕ Создать напоминание"
BTN_LIST   = "🗓 Список"
BTN_TZ     = "🌍 Часовой пояс"
BTN_HELP   = "ℹ️ Помощь"

LIST_KEYWORDS = ("список", "покажи", "покажи напоминания", "что запланировано")
DELETE_RE = re.compile(r"(удали|отмени|сотри)\s*(?:напоминание\s*)?#?(\d+)", re.IGNORECASE)
TZ_KEYWORDS = ("часовой пояс", "таймзона", "timezone")

CITY_TZ_MAP = {
    "москва": "Europe/Moscow",
    "московское время": "Europe/Moscow",
    "киев": "Europe/Kyiv",
    "питер": "Europe/Moscow",
    "спб": "Europe/Moscow",
    "минск": "Europe/Minsk",
}

TRIGGER_CREATE = ("напомни", "напомнить", "поставь", "создай", "создать напоминание")

# ---------- UI ----------
def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_CREATE), KeyboardButton(BTN_LIST)],
            [KeyboardButton(BTN_TZ), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True,
    )

def build_inline_kb(rid: int) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("⏰ +10 мин", callback_data=f"snooze:{rid}:10"),
            InlineKeyboardButton("⏰ +30 мин", callback_data=f"snooze:{rid}:30"),
        ],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{rid}")],
    ]
    return InlineKeyboardMarkup(kb)

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Нет BOT_TOKEN. Добавь его в .env")

# ---------- БД ----------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL DEFAULT 'UTC'
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_ts INTEGER NOT NULL,
            created_ts INTEGER NOT NULL
        )""")
        conn.commit()

def get_user_tz(user_id: int) -> str:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT tz FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        return row[0] if row else DEFAULT_TZ

def set_user_tz(user_id: int, tz: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO users(user_id, tz) VALUES(?, ?)
        ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz
        """, (user_id, tz))
        conn.commit()

def add_reminder(user_id: int, chat_id: int, text: str, due_ts: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO reminders(user_id, chat_id, text, due_ts, created_ts)
        VALUES(?, ?, ?, ?, ?)
        """, (user_id, chat_id, text, due_ts, int(time.time())))
        conn.commit()
        return c.lastrowid

def list_reminders(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        SELECT id, text, due_ts FROM reminders
        WHERE user_id=? ORDER BY due_ts ASC
        """, (user_id,))
        return c.fetchall()

def get_reminder(reminder_id: int) -> Optional[tuple]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT id, user_id, chat_id, text, due_ts FROM reminders WHERE id=?", (reminder_id,))
        return c.fetchone()

def delete_reminder(user_id: int, reminder_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (reminder_id, user_id))
        conn.commit()
        return c.rowcount > 0

def update_reminder_ts(reminder_id: int, user_id: int, new_due_ts: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("UPDATE reminders SET due_ts=? WHERE id=? AND user_id=?", (new_due_ts, reminder_id, user_id))
        conn.commit()
        return c.rowcount > 0

def get_due_reminders(after_ts: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        SELECT id, user_id, chat_id, text, due_ts FROM reminders
        WHERE due_ts >= ?
        """, (after_ts,))
        return c.fetchall()

# ---------- Безопасная отправка сообщений ----------
async def safe_send(bot, chat_id: int, text: str, reply_markup=None, parse_mode=None):
    # Один быстрый ретрай на типовые сетевые ошибки
    try:
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except RetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 2)) + 1)
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except (TimedOut, NetworkError):
        await asyncio.sleep(2)
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)

async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode=None):
    return await safe_send(context.bot, update.effective_chat.id, text, reply_markup, parse_mode)

# ---------- Парсинг времени ----------
def _to_utc(dt: datetime, user_tz: str) -> datetime:
    if dt.tzinfo is None:
        dt = pytz.timezone(user_tz).localize(dt)
    return dt.astimezone(pytz.UTC)

def extract_when_and_text(raw: str, user_tz: str) -> Optional[Tuple[datetime, str]]:
    settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": user_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "DATE_ORDER": "DMY",
    }
    found = search_dates(raw, languages=["ru", "en"], settings=settings)
    if not found:
        return None
    matched_text, dt = found[0]
    when_utc = _to_utc(dt, user_tz)
    text = raw.replace(matched_text, "").strip(" —-–—:.,;()[]").strip()
    if not text:
        text = raw
    return when_utc, text

# ---------- Планировщик ----------
scheduler = AsyncIOScheduler()

async def fire_reminder(application: Application, reminder_id: int, user_id: int, chat_id: int, text: str):
    delete_reminder(user_id, reminder_id)
    await safe_send(application.bot, chat_id, f"🔔 Напоминание:\n{text}")

def schedule_reminder(application: Application, reminder_id: int, user_id: int, chat_id: int, text: str, due_ts: int):
    run_dt = datetime.fromtimestamp(due_ts, tz=pytz.UTC)
    scheduler.add_job(
        fire_reminder,
        trigger=DateTrigger(run_date=run_dt),
        kwargs={"application": application, "reminder_id": reminder_id, "user_id": user_id, "chat_id": chat_id, "text": text},
        id=f"reminder_{reminder_id}",
        replace_existing=True,
        misfire_grace_time=60,
    )

def cancel_job(reminder_id: int):
    job = scheduler.get_job(f"reminder_{reminder_id}")
    if job:
        job.remove()

# ---------- FSM ----------
def state_clear(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    context.user_data.pop("draft_text", None)
    context.user_data.pop("draft_when_ts", None)

# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = (user.first_name or "друг").strip()
    await safe_reply(update, context,
        f"Привет, {name}! Используй кнопки внизу для создания напоминалки или пиши: "
        f"«напомни завтра в 9:00 позвонить маме», «список», «удали 3», «часовой пояс Europe/Moscow».",
        reply_markup=build_main_menu()
    )

async def set_tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await safe_reply(update, context, "Укажи часовой пояс: /tz <Region/City>\nПример: /tz Europe/Moscow", reply_markup=build_main_menu())
        return
    tz = context.args[0]
    if tz not in pytz.all_timezones:
        await safe_reply(update, context, "Неверный часовой пояс. Пример: Europe/Moscow", reply_markup=build_main_menu())
        return
    set_user_tz(user_id, tz)
    await safe_reply(update, context, f"Часовой пояс установлен: {tz}", reply_markup=build_main_menu())

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tz = get_user_tz(user.id) or DEFAULT_TZ
    rows = list_reminders(user.id)
    if not rows:
        await safe_reply(update, context, "Активных напоминаний нет.", reply_markup=build_main_menu())
        return
    lines = ["🗓 Твои напоминания:"]
    for rid, text, due_ts in rows:
        local_dt = datetime.fromtimestamp(due_ts, tz=pytz.UTC).astimezone(pytz.timezone(tz))
        lines.append(f"• #{rid}: {local_dt:%d.%m %H:%M} — {text}")
    await safe_reply(update, context, "\n".join(lines), reply_markup=build_main_menu())

async def done_cmd_impl(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: int):
    user = update.effective_user
    ok = delete_reminder(user.id, rid)
    if ok:
        cancel_job(rid)
        await safe_reply(update, context, f"Готово! Напоминание #{rid} удалено.", reply_markup=build_main_menu())
    else:
        await safe_reply(update, context, "Напоминание не найдено.", reply_markup=build_main_menu())

# ---------- Callback-кнопки ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        data = query.data
        if data.startswith("snooze:"):
            _, rid_s, mins_s = data.split(":")
            rid = int(rid_s); mins = int(mins_s)
            row = get_reminder(rid)
            if not row:
                await query.edit_message_text("Напоминание не найдено.")
                return
            _, owner_id, chat_id, text, due_ts = row
            if owner_id != user_id:
                await query.edit_message_text("Это напоминание принадлежит другому пользователю.")
                return
            new_due_ts = int(max(due_ts, int(time.time())) + mins * 60)
            if update_reminder_ts(rid, user_id, new_due_ts):
                cancel_job(rid)
                schedule_reminder(context.application, rid, owner_id, chat_id, text, new_due_ts)
                user_tz = get_user_tz(user_id) or DEFAULT_TZ
                local_dt = datetime.fromtimestamp(new_due_ts, tz=pytz.UTC).astimezone(pytz.timezone(user_tz))
                await query.edit_message_text(
                    f"⏰ Отложено: «{text}» до {local_dt:%d.%m.%Y %H:%M} ({user_tz}).\nID: {rid}",
                    reply_markup=build_inline_kb(rid),
                )
            else:
                await query.edit_message_text("Не удалось отложить напоминание.")
            return

        if data.startswith("delete:"):
            _, rid_s = data.split(":")
            rid = int(rid_s)
            row = get_reminder(rid)
            if not row:
                await query.edit_message_text("Напоминание уже удалено.")
                return
            _, owner_id, _, text, _ = row
            if owner_id != user_id:
                await query.edit_message_text("Это напоминание принадлежит другому пользователю.")
                return
            if delete_reminder(owner_id, rid):
                cancel_job(rid)
                await query.edit_message_text(f"🗑 Удалено напоминание #{rid}: «{text}».")
            else:
                await query.edit_message_text("Не удалось удалить напоминание.")
            return

        await query.edit_message_text("Неизвестное действие.")
    except Exception as e:
        # Не пытаемся здесь отвечать ещё раз — просто тихо гасим
        print(f"[callback error] {e}")

# ---------- НЛП-роутер: свободный текст ----------
def _strip_triggers(s: str) -> str:
    low = s.lower()
    for t in TRIGGER_CREATE:
        low = low.replace(t, "")
    return low.strip(" —-–—:.,;()[]").strip()

def _detect_tz_in_text(text: str) -> Optional[str]:
    m = re.search(r"\b([A-Za-z]+/[A-Za-z_]+)\b", text)
    if m and m.group(1) in pytz.all_timezones:
        return m.group(1)
    low = text.lower()
    for key, tz in CITY_TZ_MAP.items():
        if key in low:
            return tz
    return None

async def nlp_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_raw = (update.message.text or "").strip()
    text_low = text_raw.lower()

    # FSM: ждём время
    awaiting = context.user_data.get("awaiting")
    if awaiting == "time":
        user_tz = get_user_tz(update.effective_user.id) or DEFAULT_TZ
        ext = extract_when_and_text(text_raw, user_tz)
        if not ext:
            await safe_reply(update, context, "Не понял время. Например: «сегодня в 18:30» или «через 10 минут».",
                             reply_markup=build_main_menu())
            return
        when_dt, _ = ext
        draft_text = context.user_data.get("draft_text", "").strip() or "Напоминание"
        now_utc = datetime.now(pytz.UTC)
        if when_dt <= now_utc + timedelta(seconds=5):
            await safe_reply(update, context, "Время уже прошло или слишком близко. Укажи будущий момент.",
                             reply_markup=build_main_menu())
            return
        rid = add_reminder(update.effective_user.id, update.effective_chat.id, draft_text, int(when_dt.timestamp()))
        schedule_reminder(context.application, rid, update.effective_user.id, update.effective_chat.id, draft_text, int(when_dt.timestamp()))
        local_dt = when_dt.astimezone(pytz.timezone(user_tz))
        state_clear(context)
        await safe_reply(update, context,
            f"Готово! Напомню «{draft_text}» в {local_dt:%d.%m.%Y %H:%M} ({user_tz}).\nID: {rid}",
            reply_markup=build_inline_kb(rid),
        )
        return

    # FSM: ждём текст
    if awaiting == "text":
        when_ts = context.user_data.get("draft_when_ts")
        if not when_ts:
            state_clear(context)
            await safe_reply(update, context,
                "Что-то пошло не так, начнём заново. Напиши: «напомни завтра в 9:00 ...»",
                reply_markup=build_main_menu(),
            )
            return
        user_tz = get_user_tz(update.effective_user.id) or DEFAULT_TZ
        text_clean = text_raw.strip()
        if len(text_clean) < 2:
            await safe_reply(update, context, "Коротковато. О чём напомнить?", reply_markup=build_main_menu())
            return
        rid = add_reminder(update.effective_user.id, update.effective_chat.id, text_clean, when_ts)
        schedule_reminder(context.application, rid, update.effective_user.id, update.effective_chat.id, text_clean, when_ts)
        local_dt = datetime.fromtimestamp(when_ts, tz=pytz.UTC).astimezone(pytz.timezone(user_tz))
        state_clear(context)
        await safe_reply(update, context,
            f"Готово! Напомню «{text_clean}» в {local_dt:%d.%m.%Y %H:%M} ({user_tz}).\nID: {rid}",
            reply_markup=build_inline_kb(rid),
        )
        return

    # Меню/ключевые слова
    if text_raw == BTN_CREATE:
        await safe_reply(update, context, "Напиши одной фразой: «напомни завтра в 9:30 — позвонить маме».",
                         reply_markup=build_main_menu())
        return
    if text_raw == BTN_LIST or any(k in text_low for k in LIST_KEYWORDS):
        await list_cmd(update, context)
        return
    if text_raw == BTN_TZ or any(k in text_low for k in TZ_KEYWORDS):
        tz_guess = _detect_tz_in_text(text_raw)
        if tz_guess:
            set_user_tz(update.effective_user.id, tz_guess)
            await safe_reply(update, context, f"Часовой пояс установлен: {tz_guess}", reply_markup=build_main_menu())
        else:
            await safe_reply(update, context,
                "Укажи часовой пояс в формате Region/City, напр.: Europe/Moscow\n"
                "Можно так: «часовой пояс Europe/Moscow» или «московское время».",
                reply_markup=build_main_menu(),
            )
        return
    if text_raw == BTN_HELP:
        await safe_reply(update, context,
            "Пиши например:\n"
            "• «напомни через 10 минут выпить воду»\n"
            "• «список» — показать напоминания\n"
            "• «удали 3» — удалить напоминание №3\n"
            "• «часовой пояс Europe/Moscow»",
            reply_markup=build_main_menu(),
        )
        return

    # Удаление по тексту
    m = DELETE_RE.search(text_raw)
    if m:
        rid = int(m.group(2))
        await done_cmd_impl(update, context, rid)
        return

    # Прямо в тексте указан tz?
    tz_guess = _detect_tz_in_text(text_raw)
    if any(k in text_low for k in TZ_KEYWORDS) and tz_guess:
        set_user_tz(update.effective_user.id, tz_guess)
        await safe_reply(update, context, f"Часовой пояс установлен: {tz_guess}", reply_markup=build_main_menu())
        return

    # Создание напоминания из естественного текста
    user_tz = get_user_tz(update.effective_user.id) or DEFAULT_TZ
    extracted = extract_when_and_text(text_raw, user_tz)

    if extracted:
        when_dt, text_only = extracted
        if text_only.strip() == text_raw.strip() or len(text_only.strip()) < 2:
            context.user_data["awaiting"] = "text"
            context.user_data["draft_when_ts"] = int(when_dt.timestamp())
            await safe_reply(update, context, "О чём напомнить?", reply_markup=build_main_menu())
            return
        now_utc = datetime.now(pytz.UTC)
        if when_dt <= now_utc + timedelta(seconds=5):
            await safe_reply(update, context, "Время уже прошло или слишком близко. Укажи будущий момент.",
                             reply_markup=build_main_menu())
            return
        rid = add_reminder(update.effective_user.id, update.effective_chat.id, text_only, int(when_dt.timestamp()))
        schedule_reminder(context.application, rid, update.effective_user.id, update.effective_chat.id, text_only, int(when_dt.timestamp()))
        local_dt = when_dt.astimezone(pytz.timezone(user_tz))
        await safe_reply(update, context,
            f"Готово! Напомню «{text_only}» в {local_dt:%d.%m.%Y %H:%M} ({user_tz}).\nID: {rid}",
            reply_markup=build_inline_kb(rid),
        )
        return

    # Триггер без времени — спросим когда
    if any(t in text_low for t in TRIGGER_CREATE):
        draft_text = _strip_triggers(text_raw)
        if len(draft_text) < 2:
            draft_text = "Напоминание"
        context.user_data["awaiting"] = "time"
        context.user_data["draft_text"] = draft_text
        await safe_reply(update, context, "Когда напомнить? (например: «сегодня в 18:30» или «через 10 минут»)",
                         reply_markup=build_main_menu())
        return

    # Ничего не распознали
    await safe_reply(update, context,
        "Я понял не всё. Примеры:\n"
        "• «напомни завтра в 9:00 позвонить маме»\n"
        "• «список»\n"
        "• «удали 2»\n"
        "• «часовой пояс Europe/Moscow»",
        reply_markup=build_main_menu(),
    )

# ---------- Глобальный обработчик ошибок ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    # Логируем в консоль, не падаем
    print(f"[error] {type(err).__name__}: {err}")
    # На сетевые таймауты/RetryAfter не отвечаем пользователю — просто молча пережидаем

# ---------- Точка входа ----------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Показать меню"),
        BotCommand("tz", "Установить часовой пояс (формат Region/City)"),
        BotCommand("list", "Список напоминаний"),
    ])
    await app.bot.set_my_short_description("Бот-напоминалка: понимает обычные фразы без слэшей")
    await app.bot.set_my_description("Пиши: «напомни через 10 минут», «список», «удали 3», «часовой пояс Europe/Moscow».")

def main():
    init_db()

    # Настраиваем HTTP-клиент Telegram с таймаутами
    request = HTTPXRequest(
        connect_timeout=10.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=5.0
    )

    app = Application.builder().token(BOT_TOKEN).request(request).post_init(post_init).build()

    # Команды (оставлены для удобства)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tz", set_tz_cmd))
    app.add_handler(CommandHandler("list", list_cmd))

    # Инлайн-кнопки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Главный роутер свободного текста
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nlp_router))

    # Глобальный обработчик ошибок — обязательно
    app.add_error_handler(error_handler)

    scheduler.start()

    # восстановление задач после рестарта
    now_ts = int(time.time())
    for rid, user_id, chat_id, text, due_ts in get_due_reminders(now_ts):
        schedule_reminder(app, rid, user_id, chat_id, text, due_ts)

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
