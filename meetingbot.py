# bot.py
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib import request

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "meetings.db"  # always local file
TIMEZONE = ZoneInfo("Asia/Singapore")
WEEKLY_CRON = {"day_of_week": "mon", "hour": 8, "minute": 0}  # every Monday 08:00
SELF_URL = os.getenv("SELF_URL")  # optional self-ping for uptime
LOG_LEVEL = logging.INFO
# ----------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger(__name__)

# ---------------- DB ----------------
def init_db():
    con = sqlite3.connect(DB_PATH)
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

def add_meeting(chat_id, title, description, start_dt, end_dt=None):
    db_execute(
        "INSERT INTO meetings (chat_id, title, description, start_ts, end_ts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, title, description or "", start_dt.isoformat(), end_dt.isoformat() if end_dt else None, datetime.now().isoformat()),
    )
    logger.info("Added meeting for chat %s: %s", chat_id, title)

def list_meetings(chat_id):
    return db_execute(
        "SELECT id, title, description, start_ts, end_ts FROM meetings WHERE chat_id = ? ORDER BY start_ts",
        (chat_id,),
        fetch=True
    )

# ---------------- Parsing ----------------
def parse_dt(text: str):
    try:
        date_part, time_part = text.split("-", 1)
        parts = date_part.strip().split()
        if len(parts) != 4:
            return None, None
        year, month, day, start_hm = parts
        end_hm = time_part.strip()
        start_dt = datetime(int(year), int(month), int(day), int(start_hm[:2]), int(start_hm[2:]), tzinfo=TIMEZONE)
        end_dt = datetime(int(year), int(month), int(day), int(end_hm[:2]), int(end_hm[2:]), tzinfo=TIMEZONE)
        return start_dt, end_dt
    except:
        return None, None

def fmt_meeting(row):
    mid, title, desc, start_ts, end_ts = row
    start_dt = datetime.fromisoformat(start_ts).astimezone(TIMEZONE)
    s = f"id: [{mid}] {title}\n{start_dt.strftime('%Y %m %d %H%M')}"
    if end_ts:
        end_dt = datetime.fromisoformat(end_ts).astimezone(TIMEZONE)
        s += f" - {end_dt.strftime('%H%M')}"
    if desc:
        s += f"\ndesc: {desc}"
    return s

# ---------------- Bot Commands ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I'm your MeetingBot.\n"
        "Commands:\n"
        "/add - add a meeting\n"
        "/list - list meetings\n"
        "/delete <id> - delete meeting\n"
        "/weekly - send weekly schedule now\n\n"
        "Date/time format for adding: YYYY MM DD HHMM - HHMM"
    )

ADD_TITLE, ADD_DESC, ADD_START = range(3)

async def add_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send meeting title:")
    return ADD_TITLE

async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["meeting"] = {"title": update.message.text.strip()}
    await update.message.reply_text("Send a description (or /skip):")
    return ADD_DESC

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["meeting"]["description"] = update.message.text.strip()
    await update.message.reply_text("Send date/time range (YYYY MM DD HHMM - HHMM):")
    return ADD_START

async def add_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["meeting"]["description"] = ""
    await update.message.reply_text("Send date/time range (YYYY MM DD HHMM - HHMM):")
    return ADD_START

async def add_start_dt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_dt, end_dt = parse_dt(update.message.text.strip())
    if not start_dt:
        await update.message.reply_text("Couldn't parse. Format: YYYY MM DD HHMM - HHMM")
        return ADD_START
    m = context.user_data["meeting"]
    add_meeting(update.effective_chat.id, m["title"], m.get("description",""), start_dt, end_dt)
    await update.message.reply_text("Meeting added ✅")
    return ConversationHandler.END

async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("meeting", None)
    await update.message.reply_text("Add cancelled.")
    return ConversationHandler.END

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_meetings(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No meetings stored.")
        return
    out = "\n\n".join(fmt_meeting(r) for r in rows)
    await update.message.reply_text(out)

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    try:
        mid = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    rows = list_meetings(update.effective_chat.id)
    if not any(r[0]==mid for r in rows):
        await update.message.reply_text("No meeting with that ID.")
        return
    db_execute("DELETE FROM meetings WHERE id=?",(mid,))
    await update.message.reply_text("Deleted ✅")

async def send_weekly(chat_id, app):
    now = datetime.now(TIMEZONE)
    rows = db_execute(
        "SELECT id, title, description, start_ts, end_ts FROM meetings WHERE chat_id=? AND start_ts BETWEEN ? AND ? ORDER BY start_ts",
        (chat_id, now.isoformat(), (now + timedelta(days=7)).isoformat()),
        fetch=True
    )
    if not rows:
        text = "No meetings scheduled for the next 7 days."
    else:
        parts = ["Your upcoming meetings (next 7 days):"]
        for i,r in enumerate(rows,1):
            mid,title,desc,start_ts,end_ts = r
            start_dt = datetime.fromisoformat(start_ts).astimezone(TIMEZONE)
            s = f"{i}. id: [{mid}] {title}\n{start_dt.strftime('%Y %m %d %H%M')}"
            if end_ts:
                end_dt = datetime.fromisoformat(end_ts).astimezone(TIMEZONE)
                s += f" - {end_dt.strftime('%H%M')}"
            if desc:
                s += f"\ndesc: {desc}"
            parts.append(s)
        text = "\n\n".join(parts)
    await app.bot.send_message(chat_id=chat_id, text=text)

async def weekly_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_weekly(update.effective_chat.id, context.application)
    await update.message.reply_text("Weekly schedule sent.")

# ---------------- Scheduler ----------------
def scheduled_weekly_job(app):
    chat_ids = db_execute("SELECT DISTINCT chat_id FROM meetings", fetch=True)
    for (chat_id,) in chat_ids:
        app.create_task(send_weekly(chat_id, app))

def self_ping():
    if SELF_URL:
        try:
            request.urlopen(SELF_URL)
            logger.info("Self-ping successful")
        except Exception as e:
            logger.warning("Self-ping failed: %s", e)

# ---------------- Flask ----------------
flask_app = Flask("")
@flask_app.route("/")
def home():
    return "Bot is running ✅"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# ---------------- Main ----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("weekly", weekly_now))

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start_cmd)],
        states={
            ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            ADD_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc),
                CommandHandler("skip", add_skip_desc)
            ],
            ADD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_start_dt)]
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )
    app.add_handler(add_conv)

    # Scheduler
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    trigger = CronTrigger(**WEEKLY_CRON, timezone=TIMEZONE)
    scheduler.add_job(lambda: scheduled_weekly_job(app), trigger=trigger, id="weekly_schedule")
    scheduler.add_job(self_ping, 'interval', minutes=5, id="self_ping")
    scheduler.start()

    # Flask thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Run bot
    logger.info("Starting bot...")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
