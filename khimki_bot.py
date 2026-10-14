"""Application entry point. Importing modules never starts polling."""

import fcntl
import logging
import signal
from contextlib import contextmanager
from threading import Event

from requests import RequestException
from telebot import TeleBot, apihelper

from bot_service import BotService
from delivery import Dispatcher, describe_error
from settings import Settings
from storage import Store


logger = logging.getLogger(__name__)


@contextmanager
def instance_lock(database_path):
    """Prevent two workers from sending the same database's pending jobs."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(database_path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another bot process already uses this database') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def poll(bot, service, dispatcher, stop):
    while not stop.is_set():
        try:
            dispatcher.drain(stopped=stop.is_set)
            if stop.is_set():
                break
            updates = bot.get_updates(
                offset=service.store.cursor() + 1,
                timeout=15,
                long_polling_timeout=10,
                allowed_updates=['message'],
            )
            for update in updates:
                if stop.is_set():
                    break
                service.handle_update(update)
        except (apihelper.ApiException, RequestException) as exc:
            logger.warning('Telegram connection: %s', describe_error(exc))
            if isinstance(exc, apihelper.ApiTelegramException) and exc.error_code in (401, 409):
                raise RuntimeError('Invalid bot token, active webhook, or another polling instance') from None
            delay = 5
            if isinstance(exc, apihelper.ApiTelegramException) and exc.error_code == 429:
                delay = max(delay, exc.result_json.get('parameters', {}).get('retry_after', delay))
            stop.wait(delay)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    settings = Settings.load()
    apihelper.CONNECT_TIMEOUT = 10
    apihelper.READ_TIMEOUT = 20
    apihelper.proxy = {'https': settings.proxy} if settings.proxy else None
    bot = TeleBot(settings.token, threaded=False)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with instance_lock(settings.database_path):
        initial_blocks = tuple(uid for uid in settings.initial_blocked_ids if uid not in settings.admin_ids)
        store = Store(settings.database_path, initial_blocks)
        try:
            logger.info('Bot started; administrators: %d', len(settings.admin_ids))
            poll(bot, BotService(store, settings), Dispatcher(bot, store, settings.admin_ids), stop)
        finally:
            store.close()
            bot.stop_bot()
            logger.info('Bot stopped')


if __name__ == '__main__':
    main()
