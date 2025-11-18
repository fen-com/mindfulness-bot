import json
import logging
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===================== ЛОГИ =====================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ===================== КОНСТАНТЫ =====================

USERS_FILE = "users.json"

TOKEN = "8150860871:AAHxLap18Xr1ib9UoBHkHO_CvVD3h1GlLgQ"

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

# ===================== МОДЕЛЬ ПОЛЬЗОВАТЕЛЯ =====================

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

# ===================== РАБОТА С ФАЙЛОМ =====================

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        log.info("Users file not found, starting fresh")
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
            uid = int(uid_str)
        except:
            continue

        if not isinstance(data, dict):
            continue

        migrated = dict(data)

        if "tz" in migrated and "tz_offset" not in migrated:
            migrated["tz_offset"] = migrated["tz"]
        if "start" in migrated and "start_hour" not in migrated:
            migrated["start_hour"] = migrated["start"]
        if "end" in migrated and "end_hour" not in migrated:
            migrated["end_hour"] = migrated["end"]

        allow = UserSettings.__dataclass_fields__.keys()
        clean = {k: v for k, v in migrated.items() if k in allow}

        try:
            tmp[uid] = UserSettings(**clean)
        except Exception as e:
            log.error("Failed to load user %s: %s", uid, e)

    USERS = tmp
    log.info("Loaded %d users (with migration support)", len(USERS))


def save_users() -> None:
    try:
        data = {str(uid): asdict(settings) for uid, settings in USERS.items()}
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error("Failed to save users: %s", e)

# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def get_user_tz(settings: UserSettings) -> timezone:
    return timezone(timedelta(hours=settings.tz_offset))

def clear_user_jobs(app, uid: int) -> None:
    jq = app.job_queue
    scheduler = jq.scheduler
    for job in scheduler.get_jobs():
        if job.name in (f"msg_{uid}", f"midnight_{uid}"):
            job.remove()

# ===================== ПЛАНИРОВАНИЕ =====================

def plan_today(app, uid: int, settings: UserSettings) -> None:
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    start = settings.start_hour
    end = settings.end_hour
    if start >= end:
        start, end = DEFAULT_START, DEFAULT_END

    times_local = []
    for _ in range(settings.count):
        h = random.randint(start, end - 1)
        m = random.randint(0, 59)
        tloc = datetime.combine(today_local, time(h, m), tzinfo=tz)
        times_local.append(tloc)

    times_local.sort()

    settings.planned_today = len(times_local)
    settings.sent_today = 0
    settings.last_plan_date_utc = now_utc.date().isoformat()
    save_users()

    jq = app.job_queue
    for dt_loc in times_local:
        dt_utc = dt_loc.astimezone(timezone.utc)
        dt_naive = dt_utc.replace(tzinfo=None)

        jq.run_once(
            job_send_message,
            when=dt_naive,
            name=f"msg_{uid}",
            data={"uid": uid, "scheduled_utc": dt_utc.isoformat()},
            job_kwargs={
                "misfire_grace_time": 86400,
                "coalesce": False,
            },
        )
        log.info("Scheduled msg for %s at %s", uid, dt_naive.isoformat())

    log.info("[%s] %d msgs planned for today", uid, settings.planned_today)


def schedule_midnight(app, uid: int, settings: UserSettings) -> None:
    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    next_mid_local = datetime.combine(now_local.date(), time(0, 0), tzinfo=tz) + timedelta(days=1)
    next_mid_utc = next_mid_local.astimezone(timezone.utc)
    next_naive = next_mid_utc.replace(tzinfo=None)

    app.job_queue.run_once(
        job_midnight,
        when=next_naive,
        name=f"midnight_{uid}",
        data={"uid": uid},
        job_kwargs={
            "misfire_grace_time": 86400,
            "coalesce": False,
        },
    )
    log.info("[%s] midnight job -> %s", uid, next_naive.isoformat())

# ===================== JOB CALLBACKS =====================

async def job_send_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    uid = job.data["uid"]
    settings = USERS.get(uid)
    if not settings or not settings.enabled:
        return

    # ----- НОВАЯ ЛОГИКА "запланированное время +5 минут" -----
    scheduled_str = job.data.get("scheduled_utc")
    if scheduled_str:
        scheduled_dt = datetime.fromisoformat(scheduled_str)
        now_utc = datetime.now(timezone.utc)
        if now_utc > scheduled_dt + timedelta(minutes=5):
            log.info("Skipping msg for %s (too late, >5 min)", uid)
            return
    # ----------------------------------------------------------

    text = random.choice(PROMPTS)
    try:
        await context.bot.send_message(chat_id=uid, text=text)
        settings.sent_today += 1
        save_users()
        log.info("Sent msg to %s (%d/%d)", uid, settings.sent_today, settings.planned_today)
    except Exception as e:
        log.error("Failed to send msg: %s", e)


async def job_midnight(context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = context.job.data["uid"]
    app = context.application
    settings = USERS.get(uid)
    if not settings:
        return

    clear_user_jobs(app, uid)
    plan_today(app, uid, settings)
    schedule_midnight(app, uid, settings)
    log.info("Midnight run for %s", uid)

# ===================== КОМАНДЫ =====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    uid = user.id

    settings = USERS.get(uid)
    if not settings:
        settings = UserSettings()
        USERS[uid] = settings
        save_users()

    app = context.application
    clear_user_jobs(app, uid)
    plan_today(app, uid, settings)
    schedule_midnight(app, uid, settings)

    await update.message.reply_text(
        "✨ Бот запущен!\n\n"
        "Чтобы всё работало корректно — установи часовой пояс через /settz.\n"
        "И диапазон времени через /settime.\n\n"
        "Я уже работаю и буду слать уведомления каждый день.\n"
        "Посмотреть текущие настройки можно через /status."
    )


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_tz"
    await update.message.reply_text("Пришли GMT, например +11")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_time"
    await update.message.reply_text("Пришли диапазон: начало конец (пример: 9 19)")


async def cmd_setcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "set_count"
    await update.message.reply_text("Пришли количество уведомлений (3–10).")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    settings = USERS.get(uid)
    if not settings:
        await update.message.reply_text("Набери /start")
        return

    tz = get_user_tz(settings)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    scheduler = context.application.job_queue.scheduler
    upcoming = []

    for job in scheduler.get_jobs():
        if job.name == f"msg_{uid}" and job.next_run_time:
            run_loc = job.next_run_time.replace(tzinfo=timezone.utc).astimezone(tz)
            if run_loc.date() == today_local:
                upcoming.append(run_loc)

    upcoming.sort()

    planned = settings.planned_today
    sent = settings.sent_today
    remaining = max(planned - sent, 0)

    lines = [
        "📊 Статус на сегодня:\n",
        f"Часовой пояс: GMT{settings.tz_offset:+d}",
        f"Диапазон: {settings.start_hour}–{settings.end_hour}",
        f"Уведомлений в день: {settings.count}\n",
        f"Сегодня отправлено: {sent}",
        f"Осталось: {remaining}\n",
    ]

    if upcoming:
        lines.append("Ближайшие уведомления:")
        for dt in upcoming:
            mark = "👉" if dt > now_local else "✓"
            lines.append(f"{mark} {dt.strftime('%H:%M')}")
    else:
        lines.append("На сегодня уведомлений больше нет.")

    await update.message.reply_text("\n".join(lines))


# ===================== ОБРАБОТКА ТЕКСТА =====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    if not mode:
        return

    settings = USERS.get(uid)
    if not settings:
        settings = UserSettings()
        USERS[uid] = settings

    app = context.application

    # ---- SET TZ ----
    if mode == "set_tz":
        try:
            if text.startswith("GMT") or text.startswith("gmt"):
                text = text[3:].strip()
            tz_val = int(text)
        except:
            await update.message.reply_text("Неверный формат. Пример: +11")
            return

        if not (-12 <= tz_val <= 14):
            await update.message.reply_text("Диапазон GMT: -12…+14")
            return

        settings.tz_offset = tz_val
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Окей, GMT{tz_val:+d}. План обновлён.")
        return

    # ---- SET TIME ----
    if mode == "set_time":
        parts = text.replace(",", " ").split()
        if len(parts) != 2:
            await update.message.reply_text("Неверный формат. Пример: 9 19")
            return
        try:
            s = int(parts[0])
            e = int(parts[1])
        except:
            await update.message.reply_text("Нужно два числа, пример: 9 19")
            return

        if not (0 <= s < 24 and 0 < e <= 24 and s < e):
            await update.message.reply_text("Неверный диапазон. Пример: 9 19")
            return

        settings.start_hour = s
        settings.end_hour = e
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Диапазон обновлён: {s}:00–{e}:00")
        return

    # ---- SET COUNT ----
    if mode == "set_count":
        try:
            cnt = int(text)
        except:
            await update.message.reply_text("Нужна цифра, пример: 5")
            return

        if not (MIN_COUNT <= cnt <= MAX_COUNT):
            await update.message.reply_text(f"Диапазон: {MIN_COUNT}–{MAX_COUNT}")
            return

        settings.count = cnt
        save_users()

        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

        context.user_data["mode"] = None
        await update.message.reply_text(f"Теперь уведомлений: {cnt}")
        return

# ===================== STARTUP =====================

async def on_startup(app):
    load_users()
    for uid, settings in USERS.items():
        clear_user_jobs(app, uid)
        plan_today(app, uid, settings)
        schedule_midnight(app, uid, settings)

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("setcount", cmd_setcount))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()

if __name__ == "__main__":
    main()
