from telebot import TeleBot
from config import token, admin_ids
import time
from threading import Thread

bot = TeleBot(token=token)

welcome_text = """Привет! Есть что-то интересное про Химки? Присылай сюда!

P.S. Объявления про потерянные вещи или животных не публикуем."""

thanks_text = """Большое спасибо! Наши администраторы всё посмотрят и опубликуют! Если не опубликовали, пожалуйста, не обижайтесь :) P.S. Как будут еще новости - сразу же присылайте!"""

message_storage = {}

def safe_send(to, m):
    try:
        bot.send_message(to, m)
    except Exception as e:
        print("Message error:", e)

def init_storage(message):
    if message.chat.id not in message_storage:
        message_storage[message.chat.id] = {
            "messages": [],
            "last_time": time.time()
        }


@bot.message_handler(commands=['alive'])
def start(message):
    safe_send(message.chat.id, "yep")


@bot.message_handler(commands=['start', 'help'])
def start(message):
    print(message.chat.id)
    safe_send(message.chat.id, welcome_text)
    init_storage(message)


@bot.message_handler(content_types=['text', 'video', 'photo', 'voice', 'audio', 'document', 'video', 'video_note'])
def text_handler(message):
    init_storage(message)

    message_storage[message.chat.id]['messages'].append(message.message_id)
    message_storage[message.chat.id]["last_time"] = time.time()


def watcher_in_the_sky():
    global message_storage
    while True:
        try:
            for user in message_storage:
                if message_storage[user]['messages'] and abs(message_storage[user]["last_time"] - time.time()) > 2 * 60:
                    for message_id in message_storage[user]['messages']:
                        for admin_id in admin_ids:
                            bot.forward_message(admin_id, user, message_id)
                    message_storage[user]['messages'] = []
                    safe_send(user, thanks_text)
        except Exception as e:
            print("Thread exception:", e)
        time.sleep(60)

t = Thread(target=watcher_in_the_sky)
t.start()

while True:
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print("General exception:", e)