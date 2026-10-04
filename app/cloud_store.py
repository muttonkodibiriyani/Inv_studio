"""PostgreSQL and Google Cloud Storage persistence for a single Cloud Run instance.

Database mutations are serialized with one PostgreSQL advisory transaction lock so
the validation ledger and export writes keep the same consistency as SQLite's
``BEGIN IMMEDIATE``. Invoice execution is still an in-process queue: this store does
not make queued work durable, and the application must continue to mark interrupted
jobs as errors on restart.
"""

import json
import os
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet
from .authentication import actor


ADVISORY_LOCK_ID = 0x496E7653
PERSISTED_DIRS = frozenset(("uploads", "templates"))

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS credentials (key TEXT PRIMARY KEY, value BYTEA NOT NULL)",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS exports (
        id TEXT PRIMARY KEY,
        invoice_key TEXT UNIQUE NOT NULL,
        job_id TEXT UNIQUE NOT NULL,
        payload TEXT NOT NULL,
        workbook BYTEA NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        workbook BYTEA NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        id BIGSERIAL PRIMARY KEY,
        event TEXT NOT NULL,
        payload TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
)


def _required_env(name):
    value=os.getenv(name," ").strip()
    if not value:
        raise RuntimeError(f"{name} is required for cloud persistence")
    return value


def _default_connect(database_url):
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("Cloud persistence requires the psycopg package") from None
    return psycopg.connect(database_url)


def _default_storage_client():
    try:
        from google.cloud import storage
    except ImportError:
        raise RuntimeError("Cloud persistence requires the google-cloud-storage package") from None
    return storage.Client()


def _column_name(description):
    return getattr(description,"name",description[0])


class CompatRow:
    """A small sqlite3.Row-compatible view over a PostgreSQL result row."""

    def __init__(self,row,columns):
        if isinstance(row,Mapping):
            self._columns=tuple(row)
            self._values=tuple(row[name] for name in self._columns)
        else:
            self._columns=tuple(columns)
            self._values=tuple(row)
        self._by_name=dict(zip(self._columns,self._values))

    def __getitem__(self,key):
        return self._values[key] if isinstance(key,(int,slice)) else self._by_name[key]

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)


class CompatCursor:
    def __init__(self,cursor):
        self.cursor=cursor

    @property
    def columns(self):
        return tuple(_column_name(item) for item in (self.cursor.description or ()))

    def _row(self,row):
        return None if row is None else CompatRow(row,self.columns)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self._row(row)


def _translate_sql(sql):
    normalized=" ".join(sql.split())
    rewrites={
        "INSERT INTO exports VALUES (?,?,?,?,?)": (
            "INSERT INTO exports (id,invoice_key,job_id,payload,workbook) VALUES (%s,%s,%s,%s,%s)"
        ),
        "INSERT INTO batches VALUES (?,?,?)": (
            "INSERT INTO batches (id,payload,workbook) VALUES (%s,%s,%s)"
        ),
        "SELECT id,job_id,payload FROM exports ORDER BY rowid DESC": (
            "SELECT id,job_id,payload FROM exports ORDER BY created_at DESC,id DESC"
        ),
    }
    if normalized in rewrites:
        return rewrites[normalized]
    return sql.replace("?","%s")


class CompatConnection:
    def __init__(self,connection):
        self.connection=connection

    def execute(self,sql,params=()):
        cursor=self.connection.execute(_translate_sql(sql),params or ())
        return CompatCursor(cursor)


class PostgresStore:
    """Store-compatible persistence backed by PostgreSQL and a GCS bucket.

    The optional private constructor arguments exist for isolated unit tests. Normal
    application construction uses psycopg and Application Default Credentials.
    """

    def __init__(self,root:Path,*,_connect=None,_storage_client=None,_sync_templates=False):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(self.root,0o700)
        for name in ("uploads","exports","templates","work"):
            directory=self.root/name
            directory.mkdir(exist_ok=True,mode=0o700)
            os.chmod(directory,0o700)

        self.database_url=_required_env("INV_STUDIO_DATABASE_URL")
        self.bucket_name=_required_env("INV_STUDIO_BUCKET")
        vault_key=_required_env("INV_STUDIO_VAULT_KEY")
        try:
            self.cipher=Fernet(vault_key.encode())
        except (TypeError,ValueError):
            raise RuntimeError("INV_STUDIO_VAULT_KEY must be a valid Fernet key") from None

        self._connect=_connect or _default_connect
        self.storage_client=_storage_client or _default_storage_client()
        self.bucket=self.storage_client.bucket(self.bucket_name)
        self._blob_lock=threading.RLock()
        self.path=None
        self._init_schema()
        if _sync_templates:
            self.sync_templates()

    def _init_schema(self):
        with self.connection(True) as connection:
            for statement in SCHEMA:
                connection.execute(statement)

    @contextmanager
    def connection(self,transaction=False):
        raw=self._connect(self.database_url)
        try:
            if transaction:
                raw.execute("BEGIN")
                raw.execute("SELECT pg_advisory_xact_lock(%s)",(ADVISORY_LOCK_ID,))
            yield CompatConnection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def get(self,key,default=None,c=None):
        if c is None:
            with self.connection() as connection:
                return self.get(key,default,connection)
        row=c.execute("SELECT value FROM settings WHERE key=%s",(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self,key,value,c=None):
        if c is None:
            with self.connection(True) as connection:
                return self.set(key,value,connection)
        c.execute(
            """INSERT INTO settings(key,value) VALUES (%s,%s)
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
            (key,json.dumps(value)),
        )

    def secret(self,key,value=None,delete=False):
        with self.connection(True) as connection:
            if delete:
                connection.execute("DELETE FROM credentials WHERE key=%s",(key,))
                return None
            if value is not None:
                encrypted=self.cipher.encrypt(json.dumps(value).encode())
                connection.execute(
                    """INSERT INTO credentials(key,value) VALUES (%s,%s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                    (key,encrypted),
                )
                return None
            row=connection.execute("SELECT value FROM credentials WHERE key=%s",(key,)).fetchone()
            return json.loads(self.cipher.decrypt(bytes(row[0]))) if row else None

    def job(self,job_id,value=None,c=None):
        if c is None:
            with self.connection(value is not None) as connection:
                return self.job(job_id,value,connection)
        if value is not None:
            c.execute(
                """INSERT INTO jobs(id,payload) VALUES (%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                       payload=EXCLUDED.payload, updated_at=CURRENT_TIMESTAMP""",
                (job_id,json.dumps(value)),
            )
            return value
        row=c.execute("SELECT payload FROM jobs WHERE id=%s",(job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def jobs(self):
        with self.connection() as connection:
            rows=connection.execute(
                "SELECT payload FROM jobs ORDER BY updated_at DESC,id DESC LIMIT 200"
            )
            return [json.loads(row[0]) for row in rows]

    def audit(self,event,payload,c=None):
        if c is None:
            with self.connection(True) as connection:
                return self.audit(event,payload,connection)
        c.execute(
            "INSERT INTO audit(event,payload) VALUES (%s,%s)",
            (event,json.dumps({**payload,"actor":actor.get()})),
        )

    def ledger(self,c):
        return [json.loads(row[0]) for row in c.execute("SELECT payload FROM exports")]

    def _blob_target(self,path):
        target=Path(path)
        if not target.is_absolute():
            target=self.root/target
        target=target.resolve()
        try:
            relative=target.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError("Cloud blobs must stay inside the application data directory") from None
        if not relative.parts or relative.parts[0] not in PERSISTED_DIRS:
            raise ValueError("Only uploads and supplier templates are persisted in cloud storage")
        return target,relative.as_posix()

    def _download(self,blob,target):
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        temporary=target.with_name(target.name+".part-"+uuid.uuid4().hex)
        try:
            blob.download_to_filename(str(temporary))
            os.chmod(temporary,0o600)
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)

    def ensure_blob(self,path):
        target,name=self._blob_target(path)
        with self._blob_lock:
            if not target.exists():
                self._download(self.bucket.blob(name),target)
        return target

    def persist_blob(self,path):
        target,name=self._blob_target(path)
        if not target.is_file():
            raise ValueError("Only existing files can be persisted in cloud storage")
        with self._blob_lock:
            self.bucket.blob(name).upload_from_filename(str(target))
        return name

    def sync_templates(self):
        template_root=(self.root/"templates").resolve()
        with self._blob_lock:
            for blob in self.bucket.list_blobs(prefix="templates/"):
                name=PurePosixPath(blob.name)
                parts=name.parts
                if len(parts)<2 or parts[0]!="templates" or any(part in ("",".","..") for part in parts):
                    raise ValueError("Cloud template object has an unsafe name")
                target=template_root.joinpath(*parts[1:]).resolve()
                if not target.is_relative_to(template_root):
                    raise ValueError("Cloud template object has an unsafe name")
                if not target.exists():
                    self._download(blob,target)
