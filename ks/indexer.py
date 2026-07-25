"""Indexer: scan vault, diff against index by content hash, embed, store.

Usage:
    uv run python -m ks.indexer            incremental (hash diff)
    uv run python -m ks.indexer --force    reindex everything
    uv run python -m ks.indexer --verbose  per-file progress
"""

import argparse
import sys
import time
from pathlib import Path

from . import config, db, vault
from .embed import Embedder


def run_index(
    vault_dir: Path = config.VAULT_DIR,
    db_path: Path = config.DB_PATH,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    conn = db.connect(db_path)
    embedder = Embedder()
    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "chunks": 0}
    t0 = time.monotonic()

    try:
        on_disk = vault.scan_vault(vault_dir)
        known = db.indexed_state(conn)

        for relpath in on_disk:
            vf = vault.load_file(relpath, vault_dir)
            if not force and known.get(relpath) == vf.content_hash:
                stats["unchanged"] += 1
                continue
            fallback = vf.frontmatter.get("title") or relpath
            chunks = vault.chunk_body(vf.body, fallback_title=str(fallback))
            embeddings = embedder.embed_documents([c.text for c in chunks])
            db.replace_file(conn, vf, chunks, embeddings)
            stats["indexed"] += 1
            stats["chunks"] += len(chunks)
            if verbose:
                print(f"  indexed {relpath} ({len(chunks)} chunks)")

        for relpath in set(known) - set(on_disk):
            db.remove_file(conn, relpath)
            stats["removed"] += 1
            if verbose:
                print(f"  removed {relpath}")
    finally:
        embedder.close()
        conn.close()

    stats["elapsed_s"] = round(time.monotonic() - t0, 2)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Index the knowledge vault.")
    parser.add_argument("--force", action="store_true", help="reindex all files")
    parser.add_argument("--verbose", action="store_true", help="per-file output")
    parser.add_argument("--vault", type=Path, default=config.VAULT_DIR)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    args = parser.parse_args()

    if not args.vault.is_dir():
        print(f"vault not found: {args.vault}", file=sys.stderr)
        return 1

    stats = run_index(args.vault, args.db, force=args.force, verbose=args.verbose)
    print(
        f"indexed={stats['indexed']} unchanged={stats['unchanged']} "
        f"removed={stats['removed']} chunks={stats['chunks']} "
        f"elapsed={stats['elapsed_s']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
