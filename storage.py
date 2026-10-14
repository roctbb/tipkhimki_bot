"""Durable inbox/outbox. One process owns this connection and all Telegram I/O.

Incoming update effects and the polling cursor are committed together. No database
transaction spans a network request. Callers own transaction boundaries.
"""

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocked_users (
    user_id INTEGER PRIMARY KEY, blocked_by INTEGER, blocked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, due_at REAL NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_batch ON batches(user_id)
    WHERE sealed = 0 AND cancelled = 0;
CREATE INDEX IF NOT EXISTS open_batches_due ON batches(due_at) WHERE sealed=0;
CREATE INDEX IF NOT EXISTS unfinished_batches ON batches(id)
    WHERE sealed=1 AND notified=0 AND cancelled=0;
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('text', 'forward', 'reply')),
    chat_id INTEGER NOT NULL, user_id INTEGER,
    source_chat_id INTEGER, source_message_id INTEGER,
    text TEXT, batch_id INTEGER REFERENCES batches(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sent', 'failed', 'cancelled')),
    next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    header_message_id INTEGER, result_message_id INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(status, next_attempt, id);
CREATE INDEX IF NOT EXISTS batch_jobs ON jobs(batch_id, status);
CREATE INDEX IF NOT EXISTS pending_delivery_order ON jobs(chat_id, user_id, kind, id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS reply_links (
    admin_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    PRIMARY KEY(admin_id, message_id)
);
"""


class Store:
    def __init__(self, path, initial_blocked_ids=()):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise RuntimeError("Unsupported database schema version")
        self.db.executescript(SCHEMA)
        with self.db:
            if not self.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                self.db.executemany(
                    "INSERT OR IGNORE INTO blocked_users VALUES (?, NULL, 0)",
                    [(user_id,) for user_id in initial_blocked_ids],
                )
                self.db.execute("INSERT INTO metadata VALUES ('initialized', '1')")
            self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def cursor(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='update_id'").fetchone()
        return int(row[0]) if row else -1

    def set_cursor(self, update_id):
        self.db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('update_id', ?)", (str(update_id),)
        )

    def is_blocked(self, user_id):
        return self.db.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)
        ).fetchone() is not None

    def block(self, user_id, admin_id, now):
        self.db.execute(
            "INSERT OR REPLACE INTO blocked_users VALUES (?, ?, ?)",
            (user_id, admin_id, now),
        )
        self.db.execute(
            "UPDATE jobs SET status='cancelled' WHERE user_id=? AND status='pending'",
            (user_id,),
        )
        self.db.execute(
            "UPDATE batches SET cancelled=1, sealed=1 WHERE user_id=?", (user_id,)
        )

    def unblock(self, user_id):
        self.db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))

    def blocked_ids(self):
        return [row[0] for row in self.db.execute("SELECT user_id FROM blocked_users ORDER BY user_id")]

    def text(self, chat_id, text, *, user_id=None, batch_id=None):
        return self.db.execute(
            "INSERT INTO jobs(kind, chat_id, user_id, text, batch_id) VALUES ('text', ?, ?, ?, ?)",
            (chat_id, user_id, text, batch_id),
        ).lastrowid

    def seal_due(self, now):
        self.db.execute("UPDATE batches SET sealed=1 WHERE sealed=0 AND due_at<=?", (now,))

    def enqueue_news(self, user_id, message_id, admin_ids, now, delay):
        self.seal_due(now)
        batch = self.db.execute(
            "SELECT id FROM batches WHERE user_id=? AND sealed=0 AND cancelled=0", (user_id,)
        ).fetchone()
        if batch:
            batch_id = batch[0]
            self.db.execute("UPDATE batches SET due_at=? WHERE id=?", (now + delay, batch_id))
        else:
            batch_id = self.db.execute(
                "INSERT INTO batches(user_id, due_at) VALUES (?, ?)", (user_id, now + delay)
            ).lastrowid
        self.db.executemany(
            """INSERT INTO jobs(kind, chat_id, user_id, source_chat_id, source_message_id, batch_id)
               VALUES ('forward', ?, ?, ?, ?, ?)""",
            [(admin, user_id, user_id, message_id, batch_id) for admin in admin_ids],
        )

    def enqueue_reply(self, admin_id, message_id, user_id):
        return self.db.execute(
            """INSERT INTO jobs(kind, chat_id, user_id, source_chat_id, source_message_id)
               VALUES ('reply', ?, ?, ?, ?)""",
            (user_id, user_id, admin_id, message_id),
        ).lastrowid

    def link(self, admin_id, message_id, user_id):
        self.db.execute(
            "INSERT OR REPLACE INTO reply_links VALUES (?, ?, ?)", (admin_id, message_id, user_id)
        )

    def reply_target(self, admin_id, message_id):
        row = self.db.execute(
            "SELECT user_id FROM reply_links WHERE admin_id=? AND message_id=?",
            (admin_id, message_id),
        ).fetchone()
        return row[0] if row else None

    def next_job(self, now):
        return self.db.execute(
            """SELECT j.* FROM jobs j LEFT JOIN batches b ON b.id=j.batch_id
               WHERE j.status='pending' AND j.next_attempt<=?
                 AND (b.id IS NULL OR (b.sealed=1 AND b.cancelled=0))
                 AND NOT EXISTS (SELECT 1 FROM jobs earlier
                     WHERE earlier.id<j.id AND earlier.status='pending'
                       AND earlier.chat_id=j.chat_id AND earlier.user_id=j.user_id
                       AND earlier.kind=j.kind AND j.kind IN ('forward', 'reply'))
               ORDER BY j.id LIMIT 1""", (now,)
        ).fetchone()

    def complete_batches(self, thanks_text):
        batches = self.db.execute(
            """SELECT b.id, b.user_id FROM batches b
               WHERE b.sealed=1 AND b.notified=0 AND b.cancelled=0
                 AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id=b.id
                                 AND j.kind='forward' AND j.status!='sent')"""
        ).fetchall()
        for batch in batches:
            self.text(batch['user_id'], thanks_text, user_id=batch['user_id'], batch_id=batch['id'])
            self.db.execute("UPDATE batches SET notified=1 WHERE id=?", (batch['id'],))

    def retry(self, job_id, now):
        job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job['status'] != 'failed':
            return False
        if job['user_id'] and self.is_blocked(job['user_id']):
            return False
        if job['batch_id'] and self.db.execute(
            "SELECT cancelled FROM batches WHERE id=?", (job['batch_id'],)
        ).fetchone()[0]:
            return False
        self.db.execute(
            "UPDATE jobs SET status='pending', next_attempt=?, attempts=0, error=NULL WHERE id=?",
            (now, job_id),
        )
        return True

    def counts(self):
        return dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    def failures(self):
        return self.db.execute(
            "SELECT id, kind, chat_id, error FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT 20"
        ).fetchall()
