#!/usr/bin/env python3
"""Import a private gzip NDJSON reference export without touching invoice tables."""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cloud_store import CompatConnection  # noqa: E402
from app.reference_lookup import ReferenceLookup  # noqa: E402
from app.store import Store  # noqa: E402


class LookupPostgresStore:
    """Minimal Store connection surface with no invoice-ledger advisory lock."""

    def __init__(self, database_url):
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL lookup import requires the cloud dependency group") from None
        self.database_url = database_url
        self._connect = psycopg.connect

    @contextmanager
    def connection(self, transaction=False):
        raw = self._connect(self.database_url, connect_timeout=20)
        try:
            yield CompatConnection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


def parser():
    result = argparse.ArgumentParser(
        description="Atomically import private ItemMaster and PO/GRN lookup rows."
    )
    result.add_argument("archive", help="gzip NDJSON export")
    result.add_argument("manifest", help="JSON manifest with counts and source hashes")
    result.add_argument("--data-dir", default=os.getenv("INV_STUDIO_DATA", str(ROOT / ".data")),
                        help="local data directory when INV_STUDIO_DATABASE_URL is unset")
    result.add_argument("--batch-size", type=int, default=2_000)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    database_url = os.getenv("INV_STUDIO_DATABASE_URL", "").strip()
    store = LookupPostgresStore(database_url) if database_url else Store(Path(args.data_dir))
    result = ReferenceLookup(store).import_archive(args.archive, args.manifest, args.batch_size)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Reference lookup import failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
