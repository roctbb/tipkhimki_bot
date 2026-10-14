"""Retryable delivery without repeating successful recipients or saved headers."""

import logging
import time

from requests import RequestException
from telebot import apihelper

from bot_service import THANKS_TEXT


logger = logging.getLogger(__name__)


def describe_error(exc):
    # Request exception strings may contain the bot token or proxy credentials.
    if isinstance(exc, apihelper.ApiTelegramException):
        return f"Telegram {exc.error_code}: {exc.description}"[:500]
    return type(exc).__name__


class Dispatcher:
    def __init__(self, bot, store, admin_ids, clock=time.time):
        self.bot = bot
        self.store = store
        self.admin_ids = admin_ids
        self.clock = clock

    def drain(self, limit=20, stopped=lambda: False):
        for _ in range(limit):
            if stopped():
                return
            with self.store.db:
                self.store.seal_due(self.clock())
                self.store.complete_batches(THANKS_TEXT)
            job = self.store.next_job(self.clock())
            if job is None:
                return
            # Apply changed administrator settings to old pending work as well.
            cancelled = (job['user_id'] and self.store.is_blocked(job['user_id'])) or (
                job['kind'] == 'forward' and job['chat_id'] not in self.admin_ids
            ) or (job['kind'] == 'reply' and job['source_chat_id'] not in self.admin_ids) or (
                job['kind'] == 'text' and job['user_id'] is None
                and job['chat_id'] not in self.admin_ids
            )
            if cancelled:
                with self.store.db:
                    self.store.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job['id'],))
                continue
            try:
                result = self.send(job)
            except (apihelper.ApiException, RequestException) as exc:
                if isinstance(exc, apihelper.ApiTelegramException) and exc.error_code in (401, 409):
                    raise
                self.failed(job, exc)
            else:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE jobs SET status='sent', result_message_id=?, error=NULL WHERE id=?",
                        (result.message_id, job['id']),
                    )
                    if job['kind'] == 'forward':
                        self.store.link(job['chat_id'], result.message_id, job['user_id'])
                    elif job['kind'] == 'reply':
                        self.store.text(job['source_chat_id'], f"Ответ пользователю {job['user_id']} доставлен.")

    def send(self, job):
        if job['kind'] == 'text':
            return self.bot.send_message(job['chat_id'], job['text'])
        if job['kind'] == 'reply':
            # copy_message preserves text/media, without exposing the admin's profile.
            return self.bot.copy_message(job['chat_id'], job['source_chat_id'], job['source_message_id'])
        if job['header_message_id'] is None:
            header = self.bot.send_message(
                job['chat_id'], f"User ID: {job['user_id']}\n"
                "Чтобы ответить, используйте «Ответить» на этой карточке или следующем сообщении.\n"
                f"Заблокировать: /block {job['user_id']}",
            )
            with self.store.db:
                self.store.db.execute(
                    "UPDATE jobs SET header_message_id=? WHERE id=?", (header.message_id, job['id'])
                )
                self.store.link(job['chat_id'], header.message_id, job['user_id'])
        return self.bot.forward_message(job['chat_id'], job['source_chat_id'], job['source_message_id'])

    def failed(self, job, exc):
        permanent = False
        delay = min(3600, 5 * 2 ** min(job['attempts'], 10))
        if isinstance(exc, apihelper.ApiTelegramException):
            permanent = 400 <= exc.error_code < 500 and exc.error_code != 429
            if exc.error_code == 429:
                delay = max(1, exc.result_json.get('parameters', {}).get('retry_after', delay))
        error = describe_error(exc)
        logger.warning('Delivery %s: %s; %s', job['id'], error,
                       'failed' if permanent else f'retry in {delay}s')
        with self.store.db:
            self.store.db.execute(
                """UPDATE jobs SET status=?, attempts=attempts+1, next_attempt=?, error=? WHERE id=?""",
                ('failed' if permanent else 'pending', self.clock() + delay, error, job['id']),
            )
            if permanent and job['kind'] in ('forward', 'reply'):
                admin = job['chat_id'] if job['kind'] == 'forward' else job['source_chat_id']
                self.store.text(admin, f"Не удалось доставить сообщение #{job['id']} "
                                f"для пользователя {job['user_id']}.\n{error}\n"
                                f"После устранения причины: /retry {job['id']}")
