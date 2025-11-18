import os
import json
import random
import pytz
import logging
from datetime import datetime, timedelta, timezone, time
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from flask import Flask, request

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------- LOGGING ----------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------- CONSTANTS ----------------------

USERS_FILE = "users.json"

TOKEN = os.getenv("BOT_TOKEN")   # Render env variable
WEBHOOK_URL = "https://mindfulness-bot.onrender.com/webhook"  # ← поменяй на свой домен!

MIN_COUNT = 3
MAX_COUNT = 10

DEFAULT_TZ = 0
DEFAULT_START = 9
DEFAULT_END = 19
DEFAULT_COUNT = 5

PROMPTS = [
    "Сделай паузу и три глубоких вдоха-выдоха.",
    "Проверь тело: где сейчас напряжение? Мягко расслабь.",
    "На 10 секунд просто посмотри вокруг, ничего не меняя.",
    "Заметь 3 звука, которые слышишь прямо сейчас.",
    "Чем бы ты занялся, если бы был на 5% более осознанным прямо сейчас?",
]

# ---------------------- DATA MODEL ----------------------

@dataclass
class UserSettings:
    tz_offset: int = DEFAULT_TZ
    start_hour: int = DEFAULT_START
    end_hour: int = DEFAULT_END
    count: int = DEFAULT_COUNT
    enabled: bool = True

    planned_today: int = 0
    sent_today: int = 0
    last_plan_date_utc: Optional[str] = None


USERS: Dict[int, UserSettings] = {}

# ---------------------- USER STORAGE ----------------------

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        USERS = {}
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log.error("Failed to load users: %s", e)
        USERS = {}
        return

    tmp = {}
    for uid_str, data in raw.items():
        try:
            tmp[int(uid_str)] = UserSettings(**data)
        except Exception as e:
            log.error("Bad user record: %s", e)

    USERS = tmp
    log.info("Loaded %d users", len(USERS))


def save_users() -> None:
    try:
        data = {str(uid): asdict(s) for uid, s in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save users: %s", e)

# ---------------------- HELPERS ----------------------

def get_user_tz(s: UserSettings):
    return timezone(timedelta(hours=s.tz_offset))


def clear_jobs(app, uid: int):
    for job in app.job_queue.scheduler.get_jobs():
        if job.name in (f"msg_{uid}", f"midnight_{uid}"):
            job.remove()


def schedule_today(app, uid: int, s: UserSettings):
    tz = get_user_tz(s)
    now_utc = datetime.now(timezone.utc)
    now_loc = now_utc.astimezone(tz)

    start, end = s.start_hour, s.end_hour
    if start >= end:
        start, end = DEFAULT_START, DEFAULT_END

    today = now_loc.date()
    times_loc = []

    for _ in range(s.count):
        h = random.randint(start, end - 1)
        m = random.randint(0, 59)
        dt = datetime.combine(today, time(h, m), tzinfo=tz)

        # задержка отправки 5 минут, как просил
        dt += timedelta(minutes=5)

        times_loc.append(dt)

    times_loc.sort()

    s.planned_today = len(times_loc)
    s.sent_today = 0
    s.last_plan_date_utc = now_utc.date().isoformat()
    save_users()

    for dt_loc in times_loc:
        dt_utc = dt_loc.astimezone(timezone.utc).replace(tzinfo=None)
        app.job_queue.run_once(
            job_send_message,
            when=dt_utc,
            name=f"msg_{uid}",
            data={"uid": uid},
            job_kwargs={
                "misfire_grace_time": 60*60*24,
                "coalesce": False,
            },
        )
        log.info("Planned %s at %s", uid, dt_utc)


def schedule_midnight(app, uid: int, s: UserSettings):
    tz = get_user_tz(s)
    now = datetime.now(timezone.utc).astimezone(tz)
    next_mid = datetime.combine(now.date(), time(0,0), tzinfo=tz) + timedelta(days=1)
    dt = next_mid.astimezone(timezone.utc).replace(tzinfo=None)

    app.job_queue.run_once(
        job_midnight,
        when=dt,
        name=f"midnight_{uid}",
        data={"uid": uid},
        job_kwargs={"misfire_grace_time": 60*60*24, "coalesce": False},
    )

# ---------------------- JOBS ----------------------

async def job_send_message(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.data["uid"]
    s = USERS.get(uid)
    if not s or not s.enabled:
        return

    text = random.choice(PROMPTS)
    try:
        await ctx.bot.send_message(uid, text)
        s.sent_today += 1
        save_users()
    except Exception as e:
        log.error("Send fail: %s", e)


async def job_midnight(ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.job.data["uid"]
    app = ctx.application
    s = USERS.get(uid)
    if not s:
        return

    clear_jobs(app, uid)
    schedule_today(app, uid, s)
    schedule_midnight(app, uid, s)


async def job_ping(ctx: ContextTypes.DEFAULT_TYPE):
    """Пинг Render каждые 10 минут"""
    import requests
    try:
        requests.get(os.getenv("PING_URL", WEBHOOK_URL.replace("/webhook", "/ping")))
    except:
        pass

# ---------------------- COMMANDS ----------------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        USERS[uid] = UserSettings()
        save_users()

    s = USERS[uid]
    app = ctx.application

    clear_jobs(app, uid)
    schedule_today(app, uid, s)
    schedule_midnight(app, uid, s)

    await update.message.reply_text(
        "✨ Бот готов! Установи /settz, /settime и /setcount.\n"
        "Посмотреть настройки: /status"
    )


# SET TZ
async def settz(update: Update, ctx):
    ctx.user_data["mode"] = "tz"
    await update.message.reply_text("Пришли GMT, например +11")


# SET TIME
async def settime(update: Update, ctx):
    ctx.user_data["mode"] = "time"
    await update.message.reply_text("Пришли диапазон: начало конец (9 19)")


# SET COUNT
async def setcount(update: Update, ctx):
    ctx.user_data["mode"] = "count"
    await update.message.reply_text("Пришли количество уведомлений (3–10)")


# STATUS
async def status(update: Update, ctx):
    uid = update.effective_user.id
    s = USERS.get(uid)
    if not s:
        await update.message.reply_text("Нажми /start")
        return

    tz = get_user_tz(s)
    now_loc = datetime.now(timezone.utc).astimezone(tz)

    jobs = []
    for job in ctx.application.job_queue.scheduler.get_jobs():
        if job.name == f"msg_{uid}" and job.next_run_time:
            loc = job.next_run_time.replace(tzinfo=timezone.utc).astimezone(tz)
            jobs.append(loc)

    jobs.sort()

    text = (
        f"📊 Статус:\n"
        f"Часовой пояс: GMT{s.tz_offset:+d}\n"
        f"Диапазон: {s.start_hour}–{s.end_hour}\n"
        f"Уведомлений: {s.count}\n\n"
        f"Отправлено сегодня: {s.sent_today}\n"
        f"Осталось: {max(s.planned_today - s.sent_today, 0)}\n"
    )

    if jobs:
        text += "\nБлижайшие уведомления:\n"
        for dt in jobs:
            text += f"• {dt.strftime('%H:%M')}\n"

    await update.message.reply_text(text)


# Handle text input for commands
async def handle(update: Update, ctx):
    if not update.message:
        return

    uid = update.effective_user.id
    s = USERS.get(uid)
    app = ctx.application

    mode = ctx.user_data.get("mode")
    if not mode:
        return

    txt = update.message.text.strip()

    # SET TZ
    if mode == "tz":
        try:
            if txt.startswith("GMT") or txt.startswith("gmt"):
                txt = txt[3:].strip()
            val = int(txt)
        except:
            return await update.message.reply_text("Неверный формат. Пример: +11")

        if not -12 <= val <= 14:
            return await update.message.reply_text("Диапазон GMT от -12 до +14.")

        s.tz_offset = val
        save_users()

        clear_jobs(app, uid)
        schedule_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        return await update.message.reply_text(f"Часовой пояс обновлён: GMT{val:+d}")

    # SET TIME
    if mode == "time":
        parts = txt.split()
        if len(parts) != 2:
            return await update.message.reply_text("Формат: 9 19")

        try:
            start_h = int(parts[0])
            end_h = int(parts[1])
        except:
            return await update.message.reply_text("Только числа: 9 19")

        if not (0 <= start_h <= 23 and 0 <= end_h <= 24 and start_h < end_h):
            return await update.message.reply_text("Начало < конец, пример 9 19")

        s.start_hour = start_h
        s.end_hour = end_h
        save_users()

        clear_jobs(app, uid)
        schedule_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        return await update.message.reply_text(f"Диапазон обновлён: {start_h}:00–{end_h}:00")

    # SET COUNT
    if mode == "count":
        try:
            cnt = int(txt)
        except:
            return await update.message.reply_text("Цифрой. Пример: 5")

        if not (MIN_COUNT <= cnt <= MAX_COUNT):
            return await update.message.reply_text("От 3 до 10.")

        s.count = cnt
        save_users()

        clear_jobs(app, uid)
        schedule_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        return await update.message.reply_text(f"Теперь {cnt} уведомлений в день!")

# ---------------------- FLASK SERVER (WEBHOOK) ----------------------

app = Flask(__name__)

@app.route("/ping")
def ping():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook_handler():
    data = request.get_json()
    if data:
        update = Update.de_json(data, application.bot)
        application.update_queue.put_nowait(update)
    return "ok", 200

# ---------------------- START APPLICATION ----------------------

def start_bot():
    global application

    application = Application.builder().token(TOKEN).concurrent_updates(True).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settz", settz))
    application.add_handler(CommandHandler("settime", settime))
    application.add_handler(CommandHandler("setcount", setcount))
    application.add_handler(CommandHandler("status", status))

    # Text
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # Load users and schedule jobs
    load_users()
    for uid, s in USERS.items():
        clear_jobs(application, uid)
        schedule_today(application, uid, s)
        schedule_midnight(application, uid, s)

    # Autoping every 10 min
    application.job_queue.run_repeating(job_ping, interval=600, first=10)

    # Start webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
    )

start_bot()

