# bot.py
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from urllib import request

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# ----- Configuration -----
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "meetings.db")
LOG_LEVEL = logging.INFO
TIMEZONE = ZoneInfo("Asia/Singapore")
WEEKLY_CRON = {"day_of_week": "mon", "hour": 8, "minute": 0}  # every Monday 08:00
SELF_URL = os.getenv("SELF_URL")  # URL for uptime ping
# --------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)

# Conversation states
(ADD_TITLE, ADD_DESC, ADD_START, ADD_END, EDIT_SELECT, EDIT_FIELD, EDIT_VALUE) = range(7)

# ----- DB helpers -----
def init_db():
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    con.commit()
    con.close()

def db_execute(query, params=(), fetch=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(query, params)
    if fetch:
        rows = cur.fetchall()
        con.close()
        return rows
    else:
        con.commit()
        con.close()
        return None

def add_meeting(chat_id: int, title: str, description: str, start_dt: datetime, end_dt: datetime | None):
    db_execute(
        "INSERT INTO meetings (chat_id, title, description, start_ts, end_ts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, title, description or "", start_dt.isoformat(), end_dt.isoformat() if end_dt else None, datetime.now().isoformat()),
    )
    logger.info("Added meeting for chat %s: %s", chat_id, title)

def list_meetings_for_chat(chat_id: int):
    return db_execute(
        "SELECT id, title, description, start_ts, end_ts FROM meetings WHERE chat_id = ? ORDER BY start_ts",
        (chat_id,),
        fetch=True
    )

def get_meeting(meeting_id: int):
    rows = db_execute(
        "SELECT id, chat_id, title, description, start_ts, end_ts FROM meetings WHERE id = ?",
        (meeting_id,),
        fetch=True
    )
    return rows[0] if rows else None

def update_meeting_field(meeting_id: int, field: str, value):
    if field not in ("title", "description", "start_ts", "end_ts"):
        raise ValueError("invalid field")
    db_execute(f"UPDATE meetings SET {field} = ? WHERE id = ?", (value, meeting_id))
    logger.info("Updated meeting %s field %s", meeting_id, field)

def delete_meeting(meeting_id: int):
    db_execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    logger.info("Deleted meeting %s", meeting_id)

def meetings_in_range(chat_id: int, start_dt: datetime, end_dt: datetime):
    return db_execute(
        "SELECT id, title, description, start_ts, end_ts FROM meetings WHERE chat_id = ? AND start_ts >= ? AND start_ts <= ? ORDER BY start_ts",
        (chat_id, start_dt.isoformat(), end_dt.isoformat()),
        fetch=True
    )

# ----- Parsing -----
def parse_dt(text: str):
    """Parse YYYY MM DD HHMM - HHMM"""
    try:
        if "-" not in text:
            return None, None
        date_part, time_part = text.split("-", 1)
        parts = date_part.strip().split()
        if len(parts) != 4:
            return None, None
        year, month, day, start_hm = parts
        end_hm = time_part.strip()

        start_dt = datetime(int(year), int(month), int(day), int(start_hm[:2]), int(start_hm[2:]), tzinfo=TIMEZONE)
        end_dt = datetime(int(year), int(month), int(day), int(end_hm[:2]), int(end_hm[2:]), tzinfo=TIMEZONE)
        return start_dt, end_dt
    except Exception:
        return None, None

def parse_single_dt(text: str) -> datetime | None:
    """Parse single datetime YYYY MM DD HHMM"""
    try:
        parts = text.strip().split()
        if len(parts) != 4:
            return None
        year, month, day, hm = parts
        return datetime(int(year), int(month), int(day), int(hm[:2]), int(hm[2:]), tzinfo=TIMEZONE)
    except Exception:
        return None

def fmt_meeting_row(row) -> str:
    """Format meeting for list command"""
    mid, title, desc, start_ts, end_ts = row
    start_dt = datetime.fromisoformat(start_ts).astimezone(TIMEZONE)
    line = f"id: [{mid}] {title}\n{start_dt.strftime('%Y %m %d %H%M')}"
    if end_ts:
        end_dt = datetime.fromisoformat(end_ts).astimezone(TIMEZONE)
        line += f" - {end_dt.strftime('%H%M')}"
    if desc:
        line += f"\ndesc: {desc}"
    return line

# ----- Bot Commands -----
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your MeetingBot.\nCommands:\n"
        "/add - add a meeting\n"
        "/list - list meetings\n"
        "/delete <id> - delete meeting\n"
        "/edit <id> - edit meeting\n"
        "/weekly - send weekly schedule now\n\n"
        "Date/time format for adding: YYYY MM DD HHMM - HHMM"
    )

# ADD conversation
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send meeting title:")
    return ADD_TITLE

async def add_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_meeting"] = {"title": update.message.text.strip()}
    await update.message.reply_text("Send a description (or /skip):")
    return ADD_DESC

async def add_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_meeting"]["description"] = update.message.text.strip()
    await update.message.reply_text("Send date/time range (YYYY MM DD HHMM - HHMM):")
    return ADD_START

async def add_skip_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_meeting"]["description"] = ""
    await update.message.reply_text("Send date/time range (YYYY MM DD HHMM - HHMM):")
    return ADD_START

async def add_start_dt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    start_dt, end_dt = parse_dt(text)
    if not start_dt:
        await update.message.reply_text("Couldn't parse. Format: YYYY MM DD HHMM - HHMM")
        return ADD_START

    nm = ctx.user_data.get("new_meeting", {})
    nm["start"] = start_dt
    nm["end"] = end_dt
    add_meeting(update.effective_chat.id, nm["title"], nm.get("description", ""), start_dt, end_dt)
    await update.message.reply_text("Meeting added ✅")
    return ConversationHandler.END

async def add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_meeting", None)
    await update.message.reply_text("Add cancelled.")
    return ConversationHandler.END

# LIST
async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = list_meetings_for_chat(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No meetings stored.")
        return
    out = [fmt_meeting_row(r) for r in rows]
    await update.message.reply_text("\n\n".join(out))

# DELETE
async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    try:
        mid = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    m = get_meeting(mid)
    if not m:
        await update.message.reply_text("No meeting with that ID.")
        return
    if m[1] != update.effective_chat.id:
        await update.message.reply_text("You can only delete meetings in this chat.")
        return
    delete_meeting(mid)
    await update.message.reply_text("Deleted ✅")

# WEEKLY
async def weekly_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_weekly_for_chat(update.effective_chat.id, ctx)
    await update.message.reply_text("Weekly schedule sent.")

async def send_weekly_for_chat(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE | None = None):
    now = datetime.now(TIMEZONE)
    start = now
    end = now + timedelta(days=7)
    rows = meetings_in_range(chat_id, start, end)
    if not rows:
        text = "No meetings scheduled for the next 7 days."
    else:
        parts = ["Your upcoming meetings (next 7 days):"]
        for i, r in enumerate(rows, start=1):
            mid, title, desc, start_ts, end_ts = r
            start_dt = datetime.fromisoformat(start_ts).astimezone(TIMEZONE)
            if end_ts:
                end_dt = datetime.fromisoformat(end_ts).astimezone(TIMEZONE)
                time_str = f"{start_dt.strftime('%Y %m %d %H%M')} - {end_dt.strftime('%H%M')}"
            else:
                time_str = f"{start_dt.strftime('%Y %m %d %H%M')}"
            part = f"{i}. id: [{mid}] {title}\n{time_str}"
            if desc:
                part += f"\ndesc: {desc}"
            parts.append(part)
        text = "\n\n".join(parts)

    bot = None
    if isinstance(ctx, Application):
        bot = ctx.bot
    elif hasattr(ctx, "bot"):
        bot = ctx.bot
    else:
        try:
            from telegram.ext import Application as _App
            bot = _App.current().bot
        except Exception:
            bot = None

    if bot:
        await bot.send_message(chat_id=chat_id, text=text)
    else:
        logger.warning("Could not send weekly schedule to %s", chat_id)

# ----- Scheduler -----
def scheduled_weekly_job(app: Application):
    rows = db_execute("SELECT DISTINCT chat_id FROM meetings", fetch=True)
    for (chat_id,) in rows:
        app.create_task(send_weekly_for_chat(chat_id, app))

def self_ping():
    if SELF_URL:
        try:
            request.urlopen(SELF_URL)
            logger.info("Self-ping successful")
        except Exception as e:
            logger.warning("Self-ping failed: %s", e)

# ----- Flask Web Server -----
app_flask = Flask("")
@app_flask.route("/")
def home():
    return "Bot is running ✅"

def run_web():
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# ----- Main -----
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("weekly", weekly_now))

    # Add conversation
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc), CommandHandler("skip", add_skip_desc)],
            ADD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_start_dt)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )
    app.add_handler(add_conv)

    # Scheduler
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    trigger = CronTrigger(**WEEKLY_CRON, timezone=TIMEZONE)
    scheduler.add_job(lambda: scheduled_weekly_job(app), trigger=trigger, id="weekly_schedule_job")
    scheduler.add_job(self_ping, 'interval', minutes=5, id="self_ping")
    scheduler.start()
    logger.info("Scheduler started")

    # Run Flask server in thread
    threading.Thread(target=run_web).start()

    # Run bot
    logger.info("Starting bot...")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
