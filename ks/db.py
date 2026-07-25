"""SQLite storage for the KS index.

Design (build-log decision, 2026-07-10): plain SQLite, embeddings as
float32 BLOBs, brute-force cosine (dot product on normalized vectors)
in numpy. No vector extension. Frontmatter lives on `files` (one row
per file) and is joined at query time — no denormalization drift.
content_hash drives incremental reindexing.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config
from .vault import Chunk, VaultFile, as_list

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    mtime        REAL NOT NULL,
    title        TEXT,
    type         TEXT,
    status       TEXT,
    created      TEXT,
    subject      TEXT NOT NULL DEFAULT '[]',
    relation     TEXT NOT NULL DEFAULT '[]',
    source       TEXT,
    indexed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    file_path    TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    heading_path TEXT,
    text         TEXT NOT NULL,
    embedding    BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
"""


@dataclass
class SearchResult:
    score: float
    file_path: str
    heading_path: str
    text: str
    title: str | None
    type: str | None
    status: str | None
    created: str | None


def connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def indexed_state(conn: sqlite3.Connection) -> dict[str, str]:
    """Map of relpath -> content_hash for everything currently indexed."""
    return dict(conn.execute("SELECT path, content_hash FROM files"))


def replace_file(
    conn: sqlite3.Connection,
    vf: VaultFile,
    chunks: list[Chunk],
    embeddings: np.ndarray,
) -> None:
    """Atomically (re)index one file: delete old rows, insert fresh ones."""
    fm = vf.frontmatter
    created = fm.get("created")
    with conn:
        conn.execute("DELETE FROM files WHERE path = ?", (vf.relpath,))
        conn.execute(
            """INSERT INTO files
               (path, content_hash, mtime, title, type, status, created,
                subject, relation, source, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vf.relpath,
                vf.content_hash,
                vf.mtime,
                fm.get("title"),
                fm.get("type"),
                fm.get("status"),
                str(created) if created is not None else None,
                json.dumps(as_list(fm.get("subject"))),
                json.dumps(as_list(fm.get("relation"))),
                fm.get("source"),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.executemany(
            """INSERT INTO chunks
               (file_path, chunk_index, heading_path, text, embedding)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (vf.relpath, i, c.heading_path, c.text, emb.tobytes())
                for i, (c, emb) in enumerate(zip(chunks, embeddings))
            ],
        )


def remove_file(conn: sqlite3.Connection, relpath: str) -> None:
    with conn:
        conn.execute("DELETE FROM files WHERE path = ?", (relpath,))


def search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    k: int = 5,
    type_: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
) -> list[SearchResult]:
    """Filter by frontmatter in SQL, then brute-force cosine over survivors."""
    where: list[str] = []
    params: list = []
    if type_:
        where.append("f.type = ?")
        params.append(type_)
    if status:
        where.append("f.status = ?")
        params.append(status)
    if not include_archived:
        where.append("f.path NOT LIKE ?")
        params.append(config.ARCHIVE_PREFIX + "%")

    sql = (
        "SELECT c.file_path, c.heading_path, c.text, c.embedding, "
        "f.title, f.type, f.status, f.created "
        "FROM chunks c JOIN files f ON f.path = c.file_path"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    mat = np.frombuffer(
        b"".join(r[3] for r in rows), dtype=np.float32
    ).reshape(len(rows), config.EMBED_DIM)
    scores = mat @ query_vec.astype(np.float32)
    top = np.argsort(scores)[::-1][:k]

    return [
        SearchResult(
            score=float(scores[i]),
            file_path=rows[i][0],
            heading_path=rows[i][1] or "",
            text=rows[i][2],
            title=rows[i][4],
            type=rows[i][5],
            status=rows[i][6],
            created=rows[i][7],
        )
        for i in top
    ]
