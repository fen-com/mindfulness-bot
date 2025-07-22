import logging
import random
import json
import os
from datetime import time
from telegram.ext import Updater, CommandHandler

TOKEN = "8150860871:AAHxLap18Xr1ib9UoBHkHO_CvVD3h1GlLgQ"

# ✅ ЛОГИРУЕМ В ФАЙЛ
logging.basicConfig(
    filename="bot.log",
    filemode="a",  # добавляем новые логи
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DATA_FILE = "users.json"
DEFAULT_COUNT = 3
DEFAULT_START = 9
DEFAULT_END = 19

users = {}
jobs = {}
reset_jobs = {}

def load_data():
    global users
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            users = json.load(f)
    else:
        users = {}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(users, f)

def generate_random_times(count, start_hour, end_hour):
    times = []
    start_minutes = start_hour * 60
    end_minutes = end_hour * 60
    for _ in range(count):
        rand_minute = random.randint(start_minutes, end_minutes)
        h = rand_minute // 60
        m = rand_minute % 60
        times.append(time(h, m))
    return sorted(times)

def schedule_for_user(context, user_id):
    global jobs
    if user_id not in users:
        return
    chat_id = int(user_id)
    count = users[user_id]["count"]
    start_hour = users[user_id]["start"]
    end_hour = users[user_id]["end"]

    # Удаляем старые задачи
    if user_id in jobs:
        for job in jobs[user_id]:
            job.schedule_removal()
    jobs[user_id] = []

    # Генерируем новые времена
    times = generate_random_times(count, start_hour, end_hour)
    users[user_id]["times"] = [t.strftime("%H:%M") for t in times]
    save_data()

    for t in times:
        job = context.job_queue.run_daily(send_message, time=t, context=chat_id)
        jobs[user_id].append(job)

    logging.info(f"Пользователь {user_id}: новые времена {users[user_id]['times']}")

def reset_schedule(context):
    user_id = str(context.job.context)
    logging.info(f"Сброс расписания для пользователя {user_id}")
    schedule_for_user(context, user_id)

def send_message(context):
    context.bot.send_message(chat_id=context.job.context, text="Остановись и осознай, что с тобой сейчас")

def start(update, context):
    user_id = str(update.message.chat_id)
    count = DEFAULT_COUNT
    if context.args:
        try:
            count = int(context.args[0])
        except ValueError:
            update.message.reply_text("Неверный формат. Используй: /start [число сообщений]")
            return

    if user_id not in users:
        users[user_id] = {
            "count": count,
            "start": DEFAULT_START,
            "end": DEFAULT_END,
            "times": []
        }
    else:
        users[user_id]["count"] = count

    save_data()

    schedule_for_user(context, user_id)

    if user_id in reset_jobs:
        reset_jobs[user_id].schedule_removal()
    reset_jobs[user_id] = context.job_queue.run_daily(reset_schedule, time=time(0, 0), context=user_id)

    update.message.reply_text(f"Бот запущен! Сегодня будет {count} напоминаний.")
    logging.info(f"Пользователь {user_id} запустил бота с {count} напоминаниями.")

def stop(update, context):
    user_id = str(update.message.chat_id)
    if user_id in jobs:
        for job in jobs[user_id]:
            job.schedule_removal()
        del jobs[user_id]

    if user_id in reset_jobs:
        reset_jobs[user_id].schedule_removal()
        del reset_jobs[user_id]

    if user_id in users:
        del users[user_id]
        save_data()

    update.message.reply_text("Бот остановлен.")
    logging.info(f"Пользователь {user_id} остановил бота.")

def status(update, context):
    user_id = str(update.message.chat_id)
    if user_id not in users:
        update.message.reply_text("Бот не запущен.")
        return
    times = users[user_id]["times"]
    update.message.reply_text(f"Сегодняшние напоминания:\n" + "\n".join(times))

def set_time(update, context):
    user_id = str(update.message.chat_id)
    if len(context.args) != 2:
        update.message.reply_text("Используй: /settime HH HH (например, /settime 9 19)")
        return
    try:
        start = int(context.args[0])
        end = int(context.args[1])
        if 0 <= start < 24 and 0 <= end < 24 and start < end:
            if user_id in users:
                users[user_id]["start"] = start
                users[user_id]["end"] = end
                save_data()
                update.message.reply_text(f"Интервал изменён на {start}:00 - {end}:00. Перезапускаю расписание...")
                schedule_for_user(context, user_id)
                logging.info(f"Пользователь {user_id} изменил интервал на {start}-{end}.")
            else:
                update.message.reply_text("Сначала запусти бота командой /start.")
        else:
            update.message.reply_text("Некорректные значения.")
    except ValueError:
        update.message.reply_text("Неверный формат. Пример: /settime 9 19")

def main():
    load_data()
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stop", stop))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("settime", set_time))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
