import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet
from .authentication import actor


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        for name in ("uploads", "exports", "templates", "work"):
            (root / name).mkdir(exist_ok=True, mode=0o700)
        self.path = root / "studio.sqlite3"
        keyfile = root / "vault.key"
        if not keyfile.exists():
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(keyfile.read_bytes())
        with self.connection() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credentials(key TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, invoice_key TEXT UNIQUE NOT NULL,
                job_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, workbook BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, payload TEXT NOT NULL, workbook BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, event TEXT NOT NULL,
                payload TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self, transaction=False):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            if transaction:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, key, default=None, c=None):
        if c is None:
            with self.connection() as conn:
                return self.get(key, default, conn)
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value, c=None):
        if c is None:
            with self.connection(True) as conn:
                return self.set(key, value, conn)
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def secret(self, key, value=None, delete=False):
        with self.connection(True) as c:
            if delete:
                c.execute("DELETE FROM credentials WHERE key=?", (key,))
                return None
            if value is not None:
                c.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)",
                          (key, self.cipher.encrypt(json.dumps(value).encode())))
                return None
            row = c.execute("SELECT value FROM credentials WHERE key=?", (key,)).fetchone()
            return json.loads(self.cipher.decrypt(row[0])) if row else None

    def job(self, job_id, value=None, c=None):
        if c is None:
            with self.connection(value is not None) as conn:
                return self.job(job_id, value, conn)
        if value is not None:
            c.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job_id, json.dumps(value)))
            return value
        row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def jobs(self):
        with self.connection() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 200")]

    def audit(self, event, payload, c=None):
        if c is None:
            with self.connection(True) as conn:
                return self.audit(event, payload, conn)
        c.execute("INSERT INTO audit(event,payload) VALUES (?,?)", (event, json.dumps({**payload, "actor": actor.get()})))

    def ledger(self, c):
        return [json.loads(r[0]) for r in c.execute("SELECT payload FROM exports")]
