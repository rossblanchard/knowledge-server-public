"""CLI semantic search against the KS index.

Usage:
    uv run python -m ks.search "query text"
    uv run python -m ks.search "query" -k 8 --type runbook --status active
    uv run python -m ks.search "query" --include-archived

This is the M1 smoke-test surface; vault_search (FastMCP tool) wraps
the same db.search() call.
"""

import argparse
import sys
import time
from pathlib import Path

from . import config, db
from .embed import Embedder


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the knowledge vault index.")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=5, help="number of results")
    parser.add_argument("--type", dest="type_", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"index not found: {args.db} (run ks.indexer first)", file=sys.stderr)
        return 1

    embedder = Embedder()
    conn = db.connect(args.db)
    try:
        t0 = time.monotonic()
        qvec = embedder.embed_query(args.query)
        t_embed = time.monotonic() - t0

        t0 = time.monotonic()
        results = db.search(
            conn,
            qvec,
            k=args.k,
            type_=args.type_,
            status=args.status,
            include_archived=args.include_archived,
        )
        t_search = time.monotonic() - t0
    finally:
        embedder.close()
        conn.close()

    if not results:
        print("no results")
        return 0

    for r in results:
        loc = f"{r.file_path}" + (f" :: {r.heading_path}" if r.heading_path else "")
        meta = f"type={r.type} status={r.status} created={r.created}"
        print(f"[{r.score:.4f}] {loc}")
        print(f"         {meta}")
        preview = " ".join(r.text.split())
        print(f"         {preview[:180]}")
        print()

    print(
        f"timing: embed={t_embed * 1000:.0f}ms search={t_search * 1000:.0f}ms "
        f"({len(results)} of k={args.k})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
