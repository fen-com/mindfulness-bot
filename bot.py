import json
import logging
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional

from flask import Flask, request

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    JobQueue,
    CallbackContext,
    filters,
)

# =====================================================
# ЛОГИ
# =====================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# =====================================================
# КОНСТАНТЫ
# =====================================================

USERS_FILE = "users.json"

TOKEN = os.getenv("BOT_TOKEN")   # Токен из переменной Render
WEBHOOK_SECRET = "mindfulness-secret"

# ВАЖНО: URL ЗАМЕНИТЬ НА РЕАЛЬНЫЙ Render-домен
RENDER_URL = os.getenv("mindfulness-bot.onrender.com")   # например: mindfulness-bot.onrender.com

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

# =====================================================
# МОДЕЛЬ ПОЛЬЗОВАТЕЛЯ
# =====================================================

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

# =====================================================
# РАБОТА С ФАЙЛОМ
# =====================================================

def load_users():
    global USERS
    if not os.path.exists(USERS_FILE):
        USERS = {}
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        USERS = {}
        return

    tmp = {}
    for uid, v in data.items():
        try:
            tmp[int(uid)] = UserSettings(**v)
        except:
            pass

    USERS = tmp
    log.info("Loaded %d users", len(USERS))


def save_users():
    try:
        data = {str(uid): asdict(s) for uid, s in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

# =====================================================
# ВСПОМОГАТЕЛЬНОЕ
# =====================================================

def get_user_tz(settings: UserSettings):
    return timezone(timedelta(hours=settings.tz_offset))


def clear_user_jobs(app: Application, uid: int):
    jq = app.job_queue.scheduler
    for j in jq.get_jobs():
        if j.name in (f"msg_{uid}", f"midnight_{uid}"):
            j.remove()


def plan_today(app: Application, uid: int, settings: UserSettings):
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()

    start = settings.start_hour
    end = settings.end_hour
    if start >= end:
        start, end = DEFAULT_START, DEFAULT_END

    times = []
    for _ in range(settings.count):
        h = random.randint(start, end - 1)
        m = random.randint(0, 59)
        dt_local = datetime.combine(today, time(h, m), tzinfo=tz)
        times.append(dt_local)

    times.sort()

    settings.planned_today = len(times)
    settings.sent_today = 0
    settings.last_plan_date_utc = now_utc.date().isoformat()
    save_users()

    jq = app.job_queue

    for dt_local in times:
        dt_utc = dt_local.astimezone(timezone.utc).replace(tzinfo=None)

        jq.run_once(
            job_send_message,
            dt_utc,
            name=f"msg_{uid}",
            data={"uid": uid},
            job_kwargs={"misfire_grace_time": 86400, "coalesce": False},
        )
        log.info("Scheduled %s at %s", uid, dt_utc)


def schedule_midnight(app: Application, uid: int, settings: UserSettings):
    tz = get_user_tz(settings)
    now = datetime.now(timezone.utc).astimezone(tz)
    next_midnight = datetime.combine(now.date(), time(0), tzinfo=tz) + timedelta(days=1)
    dt_utc = next_midnight.astimezone(timezone.utc).replace(tzinfo=None)

    app.job_queue.run_once(
        job_midnight,
        dt_utc,
        name=f"midnight_{uid}",
        data={"uid": uid},
        job_kwargs={"misfire_grace_time": 86400},
    )
    log.info("Midnight for %s -> %s", uid, dt_utc)

# =====================================================
# JOBS
# =====================================================

async def job_send_message(ctx: CallbackContext):
    uid = ctx.job.data["uid"]
    settings = USERS.get(uid)
    if not settings:
        return

    text = random.choice(PROMPTS)
    await ctx.bot.send_message(uid, text)

    settings.sent_today += 1
    save_users()


async def job_midnight(ctx: CallbackContext):
    uid = ctx.job.data["uid"]
    app = ctx.application
    settings = USERS.get(uid)

    if not settings:
        return

    clear_user_jobs(app, uid)
    plan_today(app, uid, settings)
    schedule_midnight(app, uid, settings)

# =====================================================
# КОМАНДЫ
# =====================================================

async def cmd_start(update: Update, ctx: CallbackContext):
    user = update.effective_user
    uid = user.id

    if uid not in USERS:
        USERS[uid] = UserSettings()
        save_users()

    app = ctx.application

    clear_user_jobs(app, uid)
    plan_today(app, uid, USERS[uid])
    schedule_midnight(app, uid, USERS[uid])

    await update.message.reply_text(
        "✨ Бот запущен!\n\n"
        "Установи часовой пояс через /settz.\n"
        "И диапазон времени через /settime.\n\n"
        "Посмотреть настройки: /status"
    )

async def cmd_settz(update: Update, ctx: CallbackContext):
    ctx.user_data["mode"] = "tz"
    await update.message.reply_text("Пришли UTC, например +11")

async def cmd_settime(update: Update, ctx: CallbackContext):
    ctx.user_data["mode"] = "time"
    await update.message.reply_text("Пришли диапазон: 9 19")

async def cmd_setcount(update: Update, ctx: CallbackContext):
    ctx.user_data["mode"] = "count"
    await update.message.reply_text("Сколько уведомлений в день? (3–10)")

async def cmd_status(update: Update, ctx: CallbackContext):
    uid = update.effective_user.id
    s = USERS.get(uid)
    if not s:
        await update.message.reply_text("Нажми /start")
        return

    await update.message.reply_text(
        f"Часовой пояс: GMT{s.tz_offset:+}\n"
        f"Диапазон: {s.start_hour}–{s.end_hour}\n"
        f"Уведомлений: {s.count}\n"
        f"Отправлено: {s.sent_today}\n"
        f"Осталось: {max(s.planned_today - s.sent_today, 0)}"
    )

# =====================================================
# ТЕКСТ
# =====================================================

async def handle_text(update: Update, ctx: CallbackContext):
    uid = update.effective_user.id
    s = USERS.setdefault(uid, UserSettings())
    app = ctx.application

    mode = ctx.user_data.get("mode")
    if not mode:
        return

    msg = update.message.text.strip()

    if mode == "tz":
        try:
            val = int(msg.replace("GMT", ""))
        except:
            await update.message.reply_text("Ошибка. Пример: +11")
            return

        s.tz_offset = val
        save_users()
        clear_user_jobs(app, uid)
        plan_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        await update.message.reply_text(f"Часовой пояс установлен: GMT{val:+}")
        return

    if mode == "time":
        parts = msg.split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: 9 19")
            return

        try:
            a, b = int(parts[0]), int(parts[1])
        except:
            await update.message.reply_text("Формат: 9 19")
            return

        if a >= b:
            await update.message.reply_text("Начало < конец")
            return

        s.start_hour = a
        s.end_hour = b
        save_users()
        clear_user_jobs(app, uid)
        plan_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        await update.message.reply_text(f"Диапазон: {a}-{b}")
        return

    if mode == "count":
        try:
            cnt = int(msg)
        except:
            await update.message.reply_text("Пример: 5")
            return

        if not (3 <= cnt <= 10):
            await update.message.reply_text("От 3 до 10")
            return

        s.count = cnt
        save_users()
        clear_user_jobs(app, uid)
        plan_today(app, uid, s)
        schedule_midnight(app, uid, s)

        ctx.user_data["mode"] = None
        await update.message.reply_text(f"Буду слать {cnt} уведомлений")
        return

# =====================================================
# WEBHOOK С FLASK
# =====================================================

app_flask = Flask(__name__)
telegram_app: Application = None


@app_flask.post(f"/{WEBHOOK_SECRET}")
def webhook():
    """Добавляем update от Telegram"""
    data = request.get_json(force=True)
    update = Update.de_json(data, telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK", 200


def start_bot():
    global telegram_app

    load_users()

    telegram_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    telegram_app.job_queue.scheduler.configure(timezone="UTC")

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("settz", cmd_settz))
    telegram_app.add_handler(CommandHandler("settime", cmd_settime))
    telegram_app.add_handler(CommandHandler("setcount", cmd_setcount))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    telegram_app.initialize()
    telegram_app.start()

    hook_url = f"https://{RENDER_URL}/{WEBHOOK_SECRET}"
    telegram_app.bot.set_webhook(url=hook_url)

    telegram_app.updater.start_polling = None  # защита от polling
    log.info("Webhook set to %s", hook_url)


if __name__ == "__main__":
    start_bot()
    app_flask.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
