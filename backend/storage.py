"""SQLite session snapshots; one server worker, durable commits before publication."""
import json
import sqlite3
from pathlib import Path
from threading import RLock


class SQLiteStore:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.commit()

    def save(self, session):
        body = {k: getattr(session, k) for k in ('id', 'status', 'events', 'breakpoints', 'seq', 'state', 'metadata')}
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?)', (session.id, json.dumps(body, allow_nan=False)))

    def load(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM sessions ORDER BY rowid')]

    def close(self):
        self.db.close()
