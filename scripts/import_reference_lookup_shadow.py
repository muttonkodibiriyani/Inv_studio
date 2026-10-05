#!/usr/bin/env python3
"""Safely import a private lookup archive through resumable shadow tables."""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cloud_store import CompatConnection  # noqa: E402
from app.lookup_shadow_import import ShadowLookupImporter  # noqa: E402


class LookupPostgresStore:
    """Minimal connection surface with no invoice-ledger advisory lock."""

    def __init__(self, database_url):
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL lookup import requires the cloud dependency group") from None
        self.database_url = database_url
        self._connect = psycopg.connect

    @contextmanager
    def connection(self, transaction=False):
        del transaction
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
        description="Resumably build and atomically publish private lookup shadow tables."
    )
    result.add_argument("archive", help="gzip NDJSON export")
    result.add_argument("manifest", help="JSON manifest with counts and source hashes")
    result.add_argument("--batch-size", type=int, default=1_000)
    result.add_argument("--throttle-seconds", type=float, default=0.02)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    database_url = os.getenv("INV_STUDIO_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("INV_STUDIO_DATABASE_URL is required for shadow lookup import")
    result = ShadowLookupImporter(LookupPostgresStore(database_url)).import_archive(
        args.archive,
        args.manifest,
        batch_size=args.batch_size,
        throttle_seconds=args.throttle_seconds,
    )
    # Console receipts may be collected by deployment logging. Keep original
    # workbook names and sheets in the private database provenance only.
    source = result.get("source", {})
    receipt = {key: value for key, value in result.items() if key != "source"}
    receipt["source"] = {
        key: source[key]
        for key in ("version", "counts", "archive_sha256", "ndjson_sha256", "import_format")
        if key in source
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Reference lookup shadow import failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
