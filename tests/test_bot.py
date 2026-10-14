import os
import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from requests import ConnectionError
from telebot import TeleBot, apihelper, types

from bot_service import BotService, THANKS_TEXT
from delivery import Dispatcher
from khimki_bot import instance_lock, poll
from settings import Settings
from storage import Store


def telegram_error(code, retry_after=None):
    result = {'error_code': code, 'description': 'Test API error'}
    if retry_after is not None:
        result['parameters'] = {'retry_after': retry_after}
    return apihelper.ApiTelegramException('test', None, result)


def update(update_id, user_id=100, text='Новость', message_id=None, reply_to=None,
           chat_type='private', content_type='text', sender_id=None):
    message = {
        'message_id': message_id or update_id,
        'date': 0,
        'chat': {'id': user_id, 'type': chat_type},
        'from': {'id': sender_id or user_id, 'is_bot': False, 'first_name': 'Test'},
    }
    if content_type == 'text':
        message['text'] = text
    else:
        message['document'] = {'file_id': 'test', 'file_unique_id': 'test'}
    if reply_to is not None:
        message['reply_to_message'] = {
            'message_id': reply_to, 'date': 0, 'chat': message['chat'], 'text': 'Original',
        }
    return types.Update.de_json({'update_id': update_id, 'message': message})


class FakeBot:
    def __init__(self):
        self.calls = []
        self.attempts = []
        self.errors = defaultdict(deque)
        self.ids = defaultdict(lambda: 1000)

    def send(self, kind, chat_id, *args):
        call = (kind, chat_id, *args)
        self.attempts.append(call)
        errors = self.errors[kind, chat_id]
        if errors:
            raise errors.popleft()
        self.calls.append(call)
        self.ids[chat_id] += 1
        return SimpleNamespace(message_id=self.ids[chat_id])

    def send_message(self, chat_id, text):
        return self.send('text', chat_id, text)

    def forward_message(self, chat_id, source_chat, message_id):
        return self.send('forward', chat_id, source_chat, message_id)

    def copy_message(self, chat_id, source_chat, message_id):
        return self.send('copy', chat_id, source_chat, message_id)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.now = 0
        self.settings = Settings('123:fake', (10, 20), database_path=self.path)
        self.bot = FakeBot()
        self.open_store()
        self.addCleanup(lambda: self.store.close())

    def open_store(self, initial_blocks=()):
        self.store = Store(self.path, initial_blocks)
        self.service = BotService(self.store, self.settings, lambda: self.now)
        self.dispatcher = Dispatcher(self.bot, self.store, self.settings.admin_ids, lambda: self.now)

    def restart(self, initial_blocks=()):
        self.store.close()
        self.open_store(initial_blocks)

    def jobs(self, kind=None):
        return self.store.db.execute(
            'SELECT * FROM jobs' + (' WHERE kind=?' if kind else '') + ' ORDER BY id',
            (kind,) if kind else (),
        ).fetchall()

    def sent(self, kind):
        return [call for call in self.bot.calls if call[0] == kind]

    def thanks(self):
        return [call for call in self.bot.calls if call == ('text', 100, THANKS_TEXT)]

    def test_debounce_and_single_thanks_for_all_messages(self):
        self.service.handle_update(update(1))
        self.now = 119
        self.dispatcher.drain()
        self.assertEqual(self.bot.calls, [])
        self.service.handle_update(update(2))
        self.now = 238
        self.dispatcher.drain()
        self.assertEqual(self.bot.calls, [])
        self.now = 239
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 4)
        self.assertEqual(len(self.thanks()), 1)
        self.assertTrue(all(job['status'] == 'sent' for job in self.jobs()))

    def test_restart_keeps_queue_and_deduplicates_update(self):
        incoming = update(1)
        self.service.handle_update(incoming)
        self.restart()
        self.service.handle_update(incoming)
        self.assertEqual(len(self.jobs('forward')), 2)
        self.assertEqual(self.store.cursor(), 1)
        self.now = 121
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 2)

    def test_cursor_and_queue_roll_back_together(self):
        with patch.object(self.store, 'set_cursor', side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):
                self.service.handle_update(update(1))
        self.assertEqual(self.store.cursor(), -1)
        self.assertEqual(self.jobs(), [])
        self.service.handle_update(update(1))
        self.assertEqual(len(self.jobs('forward')), 2)

    def test_new_message_after_sealing_goes_into_new_batch(self):
        self.service.handle_update(update(1))
        self.now = 120
        self.dispatcher.drain(limit=1)
        self.service.handle_update(update(2))
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 2)
        self.now = 240
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 4)
        self.assertEqual(len(self.thanks()), 2)

    def test_partial_network_failure_retries_only_failed_recipient_after_restart(self):
        self.bot.errors['forward', 10].append(ConnectionError('sensitive URL'))
        self.service.handle_update(update(1))
        self.now = 120
        self.dispatcher.drain()
        self.assertEqual(self.sent('forward'), [('forward', 20, 100, 1)])
        self.assertEqual(self.thanks(), [])
        self.assertEqual(self.jobs('forward')[0]['error'], 'ConnectionError')
        self.restart()
        self.now = 125
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 2)
        headers = [c for c in self.sent('text') if c[2].startswith('User ID:')]
        self.assertEqual(len(headers), 2)
        self.assertEqual(len(self.thanks()), 1)

    def test_retry_after_and_message_order(self):
        self.bot.errors['forward', 10].append(telegram_error(429, 40))
        self.service.handle_update(update(1))
        self.service.handle_update(update(2))
        self.now = 120
        self.dispatcher.drain()
        self.assertFalse(any(c[1] == 10 for c in self.sent('forward')))
        self.assertEqual(self.jobs('forward')[0]['next_attempt'], 160)
        self.now = 159
        self.dispatcher.drain()
        self.assertFalse(any(c[1] == 10 for c in self.sent('forward')))
        self.now = 160
        self.dispatcher.drain()
        self.assertEqual([c[3] for c in self.sent('forward') if c[1] == 10], [1, 2])

    def test_permanent_failure_retained_and_admin_can_retry(self):
        self.bot.errors['forward', 10].append(telegram_error(400))
        self.service.handle_update(update(1))
        self.now = 120
        self.dispatcher.drain()
        failed_id = self.store.failures()[0]['id']
        self.assertEqual(self.thanks(), [])
        self.service.handle_update(update(2, 10, f'/retry {failed_id}'))
        self.dispatcher.drain()
        self.assertEqual(self.store.failures(), [])
        self.assertEqual(len(self.sent('forward')), 2)
        self.assertEqual(len(self.thanks()), 1)

    def test_block_cancels_pending_and_survives_restart(self):
        self.service.handle_update(update(1))
        self.service.handle_update(update(2, 10, '/block 100'))
        self.restart()
        self.assertTrue(self.store.is_blocked(100))
        count = len(self.jobs())
        self.service.handle_update(update(3, text='/start'))
        self.service.handle_update(update(4))
        self.assertEqual(len(self.jobs()), count)
        self.now = 200
        self.dispatcher.drain()
        self.assertEqual(self.sent('forward'), [])
        self.assertTrue(all(j['status'] == 'cancelled' for j in self.jobs('forward')))
        self.service.handle_update(update(5, 10, '/unblock 100'))
        self.service.handle_update(update(6))
        self.now = 320
        self.dispatcher.drain()
        self.assertEqual(len(self.sent('forward')), 2)

    def test_legacy_block_is_seeded_once(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        store = Store(path, (7901962333,))
        with store.db:
            store.unblock(7901962333)
        store.close()
        store = Store(path, (7901962333,))
        try:
            self.assertFalse(store.is_blocked(7901962333))
        finally:
            store.close()

    def test_admin_permissions_and_private_chat_only(self):
        self.service.handle_update(update(1, 100, '/block 200'))
        self.service.handle_update(update(2, 10, '/block 200', chat_type='group'))
        self.service.handle_update(update(3, 10, '/block 200', sender_id=100))
        self.service.handle_update(update(4, 10, '/block 20'))
        self.service.handle_update(update(5, 10, '/block 999999999999999999999999'))
        self.assertEqual(self.store.blocked_ids(), [])

    def test_reply_text_and_attachment_work_after_restart(self):
        self.service.handle_update(update(1))
        self.now = 120
        self.dispatcher.drain()
        original = self.jobs('forward')[0]
        self.restart()
        self.service.handle_update(update(2, 10, 'Ответ', reply_to=original['result_message_id']))
        self.service.handle_update(update(3, 10, reply_to=original['header_message_id'], content_type='document'))
        self.dispatcher.drain()
        self.assertEqual(self.sent('copy'), [('copy', 100, 10, 2), ('copy', 100, 10, 3)])
        self.assertEqual(len([c for c in self.sent('text') if 'Ответ пользователю' in c[2]]), 2)

    def test_reply_mapping_scoped_to_admin_chat(self):
        with self.store.db:
            self.store.link(10, 55, 100)
            self.store.link(20, 55, 200)
        self.service.handle_update(update(1, 10, reply_to=55))
        self.service.handle_update(update(2, 20, reply_to=55))
        self.service.handle_update(update(3, 10, reply_to=999))
        self.dispatcher.drain()
        self.assertEqual(self.sent('copy'), [('copy', 100, 10, 1), ('copy', 200, 20, 2)])

    def test_block_by_reply_cancels_queued_response(self):
        with self.store.db:
            self.store.link(10, 55, 100)
        self.service.handle_update(update(1, 10, reply_to=55))
        self.service.handle_update(update(2, 10, '/block', reply_to=55))
        self.service.handle_update(update(3, 10, reply_to=55))
        self.dispatcher.drain()
        self.assertEqual(self.sent('copy'), [])
        self.assertEqual(self.jobs('reply')[0]['status'], 'cancelled')

    def test_failed_reply_notifies_author_and_remains_available(self):
        with self.store.db:
            self.store.link(10, 55, 100)
        self.bot.errors['copy', 100].append(telegram_error(403))
        self.service.handle_update(update(1, 10, reply_to=55))
        self.dispatcher.drain()
        self.assertEqual(self.jobs('reply')[0]['status'], 'failed')
        self.assertTrue(any(c[1] == 10 and 'Не удалось' in c[2] for c in self.sent('text')))
        self.assertFalse(any('Ответ пользователю' in c[2] for c in self.sent('text')))

    def test_removed_admin_cannot_receive_pending_news_or_send_pending_reply(self):
        self.service.handle_update(update(1))
        with self.store.db:
            self.store.enqueue_reply(10, 15, 100)
            self.store.text(10, 'Pending admin notification')
        self.dispatcher.admin_ids = (20,)
        self.now = 120
        self.dispatcher.drain()
        self.assertEqual(self.sent('forward'), [('forward', 20, 100, 1)])
        self.assertEqual(self.sent('copy'), [])
        self.assertFalse(any(call[1] == 10 for call in self.bot.calls))

    def test_poll_acknowledges_only_committed_updates(self):
        stop = Event()
        offsets = []

        def fetch(**kwargs):
            offsets.append(kwargs['offset'])
            if len(offsets) == 1:
                return [update(12)]
            stop.set()
            return []

        self.bot.get_updates = fetch
        poll(self.bot, self.service, self.dispatcher, stop)
        self.assertEqual(offsets, [0, 13])
        self.assertEqual(len(self.jobs('forward')), 2)

    def test_telegram_library_serialization_for_deliveries(self):
        real_bot = TeleBot('123:test', threaded=False)
        requests = []

        def transport(token, endpoint, params=None, **kwargs):
            requests.append((endpoint, params))
            return {'message_id': 500 + len(requests), 'date': 0,
                    'chat': {'id': params['chat_id'], 'type': 'private'}, 'text': 'Test'}

        self.service.handle_update(update(1))
        self.now = 120
        dispatcher = Dispatcher(real_bot, self.store, self.settings.admin_ids, lambda: self.now)
        with patch('telebot.apihelper._make_request', side_effect=transport):
            dispatcher.drain()
            forwarded = self.jobs('forward')[0]
            self.service.handle_update(update(2, 10, reply_to=forwarded['result_message_id']))
            dispatcher.drain()
        copies = [params for method, params in requests if method == 'copyMessage']
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]['chat_id'], 100)
        self.assertEqual(copies[0]['from_chat_id'], 10)

    def test_database_lock_prevents_second_worker_and_releases(self):
        with instance_lock(self.path):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.path):
                    pass
        with instance_lock(self.path):
            pass


class SettingsTests(unittest.TestCase):
    @patch.dict(os.environ, {'BOT_TOKEN': '123:test', 'ADMIN_IDS': '10, 20,10',
                             'PROXY_URL': '', 'INITIAL_BLOCKED_IDS': ''}, clear=True)
    def test_environment_and_deduplicated_admins(self):
        settings = Settings.load()
        self.assertEqual(settings.admin_ids, (10, 20))
        self.assertEqual(settings.initial_blocked_ids, ())
        self.assertEqual(settings.proxy, '')

    @patch.dict(os.environ, {'BOT_TOKEN': '123:test', 'ADMIN_IDS': ''}, clear=True)
    def test_empty_admins_rejected(self):
        with self.assertRaisesRegex(ValueError, 'administrator'):
            Settings.load()

    def test_invalid_delay_rejected(self):
        for value in ['nan', 'inf', '-1']:
            with self.subTest(value=value), patch.dict(os.environ, {
                'BOT_TOKEN': '123:test', 'ADMIN_IDS': '10', 'BATCH_DELAY_SECONDS': value,
            }, clear=True):
                with self.assertRaises(ValueError):
                    Settings.load()


if __name__ == '__main__':
    unittest.main()
