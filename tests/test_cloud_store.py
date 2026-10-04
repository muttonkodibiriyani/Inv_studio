import json
import os
import stat
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.cloud_store import ADVISORY_LOCK_ID, PostgresStore
from app.authentication import actor


class FakeCursor:
    def __init__(self,columns=(),rows=()):
        self.description=[(column,) for column in columns]
        self.rows=list(rows)
        self.offset=0

    def fetchone(self):
        if self.offset>=len(self.rows):
            return None
        row=self.rows[self.offset]
        self.offset+=1
        return row

    def fetchall(self):
        rows=self.rows[self.offset:]
        self.offset=len(self.rows)
        return rows

    def __iter__(self):
        while True:
            row=self.fetchone()
            if row is None:
                return
            yield row


class MemoryDatabase:
    def __init__(self):
        self.settings={}
        self.credentials={}
        self.jobs={}
        self.exports={}
        self.batches={}
        self.audit=[]
        self.connections=[]

    def connect(self,url):
        connection=MemoryConnection(self,url)
        self.connections.append(connection)
        return connection


class MemoryConnection:
    def __init__(self,database,url):
        self.database=database
        self.url=url
        self.calls=[]
        self.committed=False
        self.rolled_back=False
        self.closed=False

    def execute(self,sql,params=()):
        normalized=" ".join(sql.split())
        params=tuple(params or ())
        self.calls.append((normalized,params))
        db=self.database
        if normalized in ("BEGIN",) or normalized.startswith("CREATE TABLE"):
            return FakeCursor()
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return FakeCursor(("pg_advisory_xact_lock",),[(None,)])
        if normalized.startswith("INSERT INTO settings"):
            db.settings[params[0]]=params[1]
            return FakeCursor()
        if normalized.startswith("SELECT value FROM settings"):
            value=db.settings.get(params[0])
            return FakeCursor(("value",),[] if value is None else [(value,)])
        if normalized.startswith("INSERT INTO credentials"):
            db.credentials[params[0]]=params[1]
            return FakeCursor()
        if normalized.startswith("DELETE FROM credentials"):
            db.credentials.pop(params[0],None)
            return FakeCursor()
        if normalized.startswith("SELECT value FROM credentials"):
            value=db.credentials.get(params[0])
            return FakeCursor(("value",),[] if value is None else [(memoryview(value),)])
        if normalized.startswith("INSERT INTO jobs"):
            db.jobs[params[0]]=params[1]
            return FakeCursor()
        if normalized.startswith("SELECT payload FROM jobs WHERE"):
            value=db.jobs.get(params[0])
            return FakeCursor(("payload",),[] if value is None else [(value,)])
        if normalized.startswith("SELECT payload FROM jobs ORDER BY"):
            return FakeCursor(("payload",),[(value,) for value in reversed(tuple(db.jobs.values()))])
        if normalized == "SELECT id,payload FROM jobs":
            return FakeCursor(("id","payload"),list(db.jobs.items()))
        if normalized.startswith("INSERT INTO audit"):
            db.audit.append((params[0],params[1]))
            return FakeCursor()
        if normalized == "SELECT * FROM audit ORDER BY id":
            rows=[(index,event,payload,"2026-01-01T00:00:00Z") for index,(event,payload) in enumerate(db.audit,1)]
            return FakeCursor(("id","event","payload","at"),rows)
        if normalized.startswith("SELECT payload FROM exports"):
            return FakeCursor(("payload",),[(row[3],) for row in db.exports.values()])
        if normalized.startswith("INSERT INTO exports (id,invoice_key,job_id,payload,workbook)"):
            db.exports[params[0]]=params
            return FakeCursor()
        if normalized.startswith("SELECT id,job_id,payload FROM exports ORDER BY created_at"):
            rows=[(row[0],row[2],row[3]) for row in reversed(tuple(db.exports.values()))]
            return FakeCursor(("id","job_id","payload"),rows)
        if normalized.startswith("SELECT workbook FROM exports WHERE id="):
            row=db.exports.get(params[0])
            return FakeCursor(("workbook",),[] if row is None else [(row[4],)])
        if normalized.startswith("INSERT INTO batches (id,payload,workbook)"):
            db.batches[params[0]]=params
            return FakeCursor()
        if normalized.startswith("SELECT workbook FROM batches WHERE id="):
            row=db.batches.get(params[0])
            return FakeCursor(("workbook",),[] if row is None else [(row[2],)])
        raise AssertionError(f"Unexpected SQL in fake database: {normalized}")

    def commit(self):
        self.committed=True

    def rollback(self):
        self.rolled_back=True

    def close(self):
        self.closed=True


class FakeBlob:
    def __init__(self,bucket,name):
        self.bucket=bucket
        self.name=name

    def upload_from_filename(self,filename):
        self.bucket.objects[self.name]=Path(filename).read_bytes()

    def download_to_filename(self,filename):
        if self.name not in self.bucket.objects:
            raise FileNotFoundError(self.name)
        Path(filename).write_bytes(self.bucket.objects[self.name])


class FakeBucket:
    def __init__(self,objects=None):
        self.objects=dict(objects or {})

    def blob(self,name):
        return FakeBlob(self,name)

    def list_blobs(self,prefix=""):
        return [FakeBlob(self,name) for name in sorted(self.objects) if name.startswith(prefix)]


class FakeStorageClient:
    def __init__(self,bucket):
        self.fake_bucket=bucket
        self.requested=[]

    def bucket(self,name):
        self.requested.append(name)
        return self.fake_bucket


@pytest.fixture
def cloud_env(monkeypatch):
    values={
        "INV_STUDIO_DATABASE_URL":"postgresql://service/database",
        "INV_STUDIO_BUCKET":"invoice-private-bucket",
        "INV_STUDIO_VAULT_KEY":Fernet.generate_key().decode(),
    }
    for key,value in values.items():
        monkeypatch.setenv(key,value)
    return values


def make_store(tmp_path,cloud_env,*,bucket=None,sync_templates=False):
    database=MemoryDatabase()
    storage=FakeStorageClient(bucket or FakeBucket())
    store=PostgresStore(
        tmp_path/"data",
        _connect=database.connect,
        _storage_client=storage,
        _sync_templates=sync_templates,
    )
    return store,database,storage


@pytest.mark.parametrize(
    "missing",
    ["INV_STUDIO_DATABASE_URL","INV_STUDIO_BUCKET","INV_STUDIO_VAULT_KEY"],
)
def test_cloud_configuration_is_required_and_no_vault_key_is_generated(
    tmp_path,cloud_env,monkeypatch,missing
):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError,match=f"{missing} is required"):
        PostgresStore(
            tmp_path/"data",
            _connect=MemoryDatabase().connect,
            _storage_client=FakeStorageClient(FakeBucket()),
            _sync_templates=False,
        )
    assert not (tmp_path/"data"/"vault.key").exists()


def test_invalid_cloud_vault_key_is_rejected(tmp_path,cloud_env,monkeypatch):
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY","not-a-fernet-key")
    with pytest.raises(RuntimeError,match="must be a valid Fernet key"):
        PostgresStore(
            tmp_path/"data",
            _connect=MemoryDatabase().connect,
            _storage_client=FakeStorageClient(FakeBucket()),
            _sync_templates=False,
        )


def test_store_api_uses_upserts_encryption_and_global_mutation_lock(tmp_path,cloud_env):
    store,database,storage=make_store(tmp_path,cloud_env)

    store.set("settings",{"provider":"openai"})
    assert store.get("settings") == {"provider":"openai"}
    secret="synthetic-cloud-key-must-not-persist"
    store.secret("openai",secret)
    assert secret.encode() not in database.credentials["openai"]
    assert store.secret("openai") == secret
    store.job("job-1",{"id":"job-1","status":"review"})
    store.job("job-2",{"id":"job-2","status":"ready"})
    assert store.job("job-1")["status"] == "review"
    assert [job["id"] for job in store.jobs()] == ["job-2","job-1"]
    actor_token=actor.set("firebase-user-123")
    try:
        store.audit("reviewed",{"job_id":"job-1"})
    finally:
        actor.reset(actor_token)
    assert json.loads(database.audit[0][1]) == {
        "job_id":"job-1",
        "actor":"firebase-user-123",
    }

    mutation_connections=[
        connection
        for connection in database.connections
        if any(sql == "BEGIN" for sql,_ in connection.calls)
    ]
    assert mutation_connections
    for connection in mutation_connections:
        assert (
            "SELECT pg_advisory_xact_lock(%s)",
            (ADVISORY_LOCK_ID,),
        ) in connection.calls
        assert connection.committed is True
        assert connection.closed is True
    assert storage.requested == [cloud_env["INV_STUDIO_BUCKET"]]
    assert not (store.root/"vault.key").exists()


def test_compat_connection_translates_main_sql_and_supports_both_row_access_styles(
    tmp_path,cloud_env
):
    store,database,_=make_store(tmp_path,cloud_env)
    receipt={"invoice_key":"SELLER|BUYER|INV-1","allocations":[]}
    with store.connection(True) as connection:
        connection.execute(
            "INSERT INTO exports VALUES (?,?,?,?,?)",
            ("export-1",receipt["invoice_key"],"job-1",json.dumps(receipt),b"xlsx"),
        )
    with store.connection() as connection:
        row=connection.execute(
            "SELECT id,job_id,payload FROM exports ORDER BY rowid DESC"
        ).fetchone()
        workbook=connection.execute(
            "SELECT workbook FROM exports WHERE id=?",("export-1",)
        ).fetchone()

    assert row[0] == row["id"] == "export-1"
    assert row["job_id"] == "job-1"
    assert bytes(workbook[0]) == b"xlsx"
    with store.connection() as connection:
        ledger=store.ledger(connection)
    assert ledger[0]["invoice_key"] == receipt["invoice_key"]
    all_sql=[sql for connection in database.connections for sql,_ in connection.calls]
    assert any("INSERT INTO exports (id,invoice_key,job_id,payload,workbook)" in sql for sql in all_sql)
    assert any("ORDER BY created_at DESC,id DESC" in sql for sql in all_sql)
    assert not any("rowid" in sql or "?" in sql for sql in all_sql)


def test_every_raw_sql_shape_used_by_main_is_compatible(tmp_path,cloud_env):
    store,_,_=make_store(tmp_path,cloud_env)
    store.job("job-1",{"id":"job-1"})
    store.audit("uploaded",{"job_id":"job-1"})
    with store.connection(True) as connection:
        jobs=connection.execute("SELECT id,payload FROM jobs").fetchall()
        connection.execute("INSERT INTO batches VALUES (?,?,?)",("batch-1","[]",b"batch"))
    with store.connection() as connection:
        batch=connection.execute(
            "SELECT workbook FROM batches WHERE id=?",("batch-1",)
        ).fetchone()
        audit_rows=list(connection.execute("SELECT * FROM audit ORDER BY id"))

    assert jobs[0]["id"] == "job-1"
    assert json.loads(jobs[0]["payload"]) == {"id":"job-1"}
    assert bytes(batch[0]) == b"batch"
    assert audit_rows[0]["event"] == "uploaded"
    assert json.loads(audit_rows[0]["payload"])["actor"] == "local-operator"
def test_transaction_rolls_back_and_closes_on_failure(tmp_path,cloud_env):
    store,database,_=make_store(tmp_path,cloud_env)
    with pytest.raises(RuntimeError,match="stop"):
        with store.connection(True):
            raise RuntimeError("stop")

    connection=database.connections[-1]
    assert connection.rolled_back is True
    assert connection.committed is False
    assert connection.closed is True


def test_gcs_upload_restore_and_startup_template_sync(tmp_path,cloud_env):
    bucket=FakeBucket({"templates/remote.yml":b"keywords: [remote]"})
    store,_,storage=make_store(tmp_path,cloud_env,bucket=bucket,sync_templates=True)
    remote=store.root/"templates"/"remote.yml"
    assert remote.read_bytes() == b"keywords: [remote]"
    assert stat.S_IMODE(remote.stat().st_mode) == 0o600

    upload=store.root/"uploads"/"invoice.pdf"
    upload.write_bytes(b"synthetic invoice")
    assert store.persist_blob(upload) == "uploads/invoice.pdf"
    assert bucket.objects["uploads/invoice.pdf"] == b"synthetic invoice"
    upload.unlink()
    assert store.ensure_blob(upload) == upload
    assert upload.read_bytes() == b"synthetic invoice"
    assert stat.S_IMODE(upload.stat().st_mode) == 0o600
    assert storage.requested == [cloud_env["INV_STUDIO_BUCKET"]]

    cache=store.root/"work"/"temporary.json"
    cache.write_text("ephemeral")
    with pytest.raises(ValueError,match="Only uploads and supplier templates"):
        store.persist_blob(cache)
    with pytest.raises(ValueError,match="inside the application data directory"):
        store.ensure_blob(tmp_path/"outside.pdf")


def test_template_sync_rejects_bucket_path_traversal(tmp_path,cloud_env):
    bucket=FakeBucket({"templates/../vault.key":b"attacker controlled"})
    with pytest.raises(ValueError,match="unsafe name"):
        make_store(tmp_path,cloud_env,bucket=bucket,sync_templates=True)
    assert not (tmp_path/"data"/"vault.key").exists()


def test_live_postgres_store_contract_when_test_database_is_configured(tmp_path,monkeypatch):
    database_url=os.getenv("INV_STUDIO_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set INV_STUDIO_TEST_DATABASE_URL to run isolated PostgreSQL integration")
    psycopg=pytest.importorskip("psycopg")
    from psycopg import sql

    schema="inv_studio_test_"+uuid.uuid4().hex
    with psycopg.connect(database_url,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect_in_schema(url):
        connection=psycopg.connect(url)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.commit()
        return connection

    monkeypatch.setenv("INV_STUDIO_DATABASE_URL",database_url)
    monkeypatch.setenv("INV_STUDIO_BUCKET","unit-test-bucket")
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY",Fernet.generate_key().decode())
    try:
        store=PostgresStore(
            tmp_path/"postgres-data",
            _connect=connect_in_schema,
            _storage_client=FakeStorageClient(FakeBucket()),
        )
        store.set("policy",{"total_tolerance":"0.01"})
        assert store.get("policy") == {"total_tolerance":"0.01"}
        store.secret("openai","sk-live-test")
        assert store.secret("openai") == "sk-live-test"
        store.job("job-live",{"id":"job-live","status":"ready"})
        assert store.job("job-live")["status"] == "ready"
        receipt={"invoice_key":"S|B|N","allocations":[]}
        with store.connection(True) as connection:
            connection.execute(
                "INSERT INTO exports VALUES (?,?,?,?,?)",
                ("export-live","S|B|N","job-live",json.dumps(receipt),b"xlsx"),
            )
            connection.execute(
                "INSERT INTO batches VALUES (?,?,?)",("batch-live","[]",b"batch")
            )
        with store.connection() as connection:
            row=connection.execute(
                "SELECT id,job_id,payload FROM exports ORDER BY rowid DESC"
            ).fetchone()
            batch=connection.execute(
                "SELECT workbook FROM batches WHERE id=?",("batch-live",)
            ).fetchone()
        assert row["id"] == row[0] == "export-live"
        assert bytes(batch[0]) == b"batch"
        assert store.jobs()[0]["id"] == "job-live"
        with store.connection() as connection:
            ledger=store.ledger(connection)
        assert ledger[0]["invoice_key"] == "S|B|N"

        with pytest.raises(RuntimeError,match="rollback"):
            with store.connection(True) as connection:
                store.set("rolled_back",True,connection)
                raise RuntimeError("rollback")
        assert store.get("rolled_back") is None
    finally:
        with psycopg.connect(database_url,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
