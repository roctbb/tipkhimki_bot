"""Telegram update routing; all effects are persisted before acknowledging an update."""

import time

from settings import parse_ids


WELCOME_TEXT = """Привет! Есть что-то интересное про Химки? Присылай сюда!

P.S. Объявления про потерянные вещи или животных не публикуем."""

THANKS_TEXT = """Большое спасибо!
Если вы прислали новость, а не объявление или рекламу, админы всё посмотрят и опубликуют! Если не опубликовали, пожалуйста, не обижайтесь :)

Как будут еще новости — сразу же присылайте!

P.S. Потерянные вещи / животных не публикуем! По рекламе писать админу, а не сюда (ссылка на видном месте в инфо канала).

Это автоматическое сообщение, на него не нужно отвечать."""

ADMIN_HELP = """Чтобы ответить пользователю, нажмите «Ответить» на пересланном сообщении или карточке с его ID и отправьте текст или вложение.

/block ID — заблокировать пользователя и отменить его очередь
/unblock ID — разблокировать пользователя
/blocked — список заблокированных
/queue — состояние очереди
/failed — последние 20 ошибок доставки
/retry НОМЕР — повторить неудачную доставку из /failed
/alive — проверить работу бота

/block и /unblock можно отправить без ID, ответом на сообщение пользователя."""

CONTENT_TYPES = {
    'text', 'video', 'photo', 'voice', 'audio', 'document', 'video_note',
    'animation', 'sticker', 'contact', 'location', 'venue',
}


class BotService:
    def __init__(self, store, settings, clock=time.time):
        self.store = store
        self.settings = settings
        self.clock = clock

    def handle_update(self, update):
        with self.store.db:
            if update.update_id <= self.store.cursor():
                return
            if update.message:
                self.handle_message(update.message)
            self.store.set_cursor(update.update_id)

    def say(self, chat_id, text, *, user_id=None):
        # Split long admin lists without crossing Telegram's message size limit.
        for start in range(0, len(text), 3500):
            self.store.text(chat_id, text[start:start + 3500], user_id=user_id)

    def handle_message(self, message):
        if message.chat.type != 'private' or not message.from_user:
            return
        user_id = message.from_user.id
        if user_id != message.chat.id or message.from_user.is_bot:
            return
        is_admin = user_id in self.settings.admin_ids
        if not is_admin and self.store.is_blocked(user_id):
            return
        text = message.text or ''
        parts = text.split()
        command = parts[0].split('@')[0].lower() if text.startswith('/') and parts else ''
        if command in ('/start', '/help'):
            self.say(user_id, ADMIN_HELP if is_admin else WELCOME_TEXT,
                     user_id=None if is_admin else user_id)
        elif command == '/alive':
            self.say(user_id, f'yep, {user_id}', user_id=None if is_admin else user_id)
        elif is_admin:
            self.handle_admin(message, command, parts[1:])
        elif command in ('/block', '/unblock', '/blocked', '/queue', '/failed', '/retry'):
            self.say(user_id, 'Команда доступна только администраторам.', user_id=user_id)
        elif message.content_type in CONTENT_TYPES:
            self.store.enqueue_news(user_id, message.message_id, self.settings.admin_ids,
                                    self.clock(), self.settings.batch_delay)

    def target_from_reply(self, message):
        if message.reply_to_message:
            return self.store.reply_target(message.chat.id, message.reply_to_message.message_id)
        return None

    def handle_admin(self, message, command, args):
        admin_id = message.chat.id
        if command in ('/block', '/unblock'):
            try:
                ids = parse_ids(args) if args else (self.target_from_reply(message),)
                if len(ids) != 1 or ids[0] is None:
                    raise ValueError()
                target = ids[0]
            except ValueError:
                self.say(admin_id, f'Использование: {command} ID или ответ на сообщение пользователя.')
                return
            if command == '/block' and target in self.settings.admin_ids:
                self.say(admin_id, 'Администратора нельзя заблокировать.')
                return
            if command == '/block':
                self.store.block(target, admin_id, self.clock())
                self.say(admin_id, f'Пользователь {target} заблокирован. Ожидающие отправки отменены.')
            else:
                self.store.unblock(target)
                self.say(admin_id, f'Пользователь {target} разблокирован. Можно присылать новые сообщения.')
        elif command == '/blocked':
            ids = self.store.blocked_ids()
            self.say(admin_id, 'Заблокированные пользователи:\n' + '\n'.join(map(str, ids))
                     if ids else 'Заблокированных пользователей нет.')
        elif command == '/queue':
            counts = self.store.counts()
            self.say(admin_id, 'Доставки в очереди:\n' + '\n'.join(
                f'{label}: {counts.get(status, 0)}' for status, label in
                [('pending', 'Ожидают'), ('sent', 'Отправлены'),
                 ('failed', 'Ошибки'), ('cancelled', 'Отменены')]))
        elif command == '/failed':
            rows = self.store.failures()
            self.say(admin_id, '\n'.join(
                f"#{r['id']} → {r['chat_id']} ({r['kind']}): {r['error']}" for r in rows
            ) if rows else 'Неудачных доставок нет.')
        elif command == '/retry':
            try:
                ids = parse_ids(args)
                retried = len(ids) == 1 and self.store.retry(ids[0], self.clock())
            except ValueError:
                retried = False
            self.say(admin_id, 'Доставка снова в очереди.' if retried else
                     'Нужен номер неудачной доставки из /failed. Отменённые и заблокированные не повторяются.')
        elif message.reply_to_message:
            target = self.target_from_reply(message)
            if target is None:
                self.say(admin_id, 'Не найден получатель. Ответьте на пересланное ботом сообщение или карточку с ID.')
            elif self.store.is_blocked(target):
                self.say(admin_id, f'Пользователь {target} заблокирован. Сначала выполните /unblock {target}.')
            elif message.content_type not in CONTENT_TYPES:
                self.say(admin_id, 'Этот тип ответа не поддерживается. Отправьте текст или вложение.')
            else:
                self.store.enqueue_reply(admin_id, message.message_id, target)
        else:
            self.say(admin_id, ADMIN_HELP)
