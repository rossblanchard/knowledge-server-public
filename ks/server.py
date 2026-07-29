"""Knowledge Server — FastMCP server over Streamable HTTP.

Exposes vault_search, vault_browse (M1), vault_write, vault_reindex
(M3, spec §5) and runs the in-process vault poller (build-log decision,
2026-07-10: poll-based freshness, no watchdog dependency). Binds
loopback only; public ingress via Cloudflare Tunnel. OAuth 2.1 AS
embedded as of M2 (ks/auth.py): DCR enabled, passphrase-gated /consent,
JWT access tokens.

M3 additions:
- vault_write: strict Schema v1.0 validation + library/** constraint
  (ks/validator.py), atomic write + per-write git commit (ks/writer.py,
  Decisions 27-29). dry_run flag = validation report + diff, no
  mutation (Decision 28). Originating OAuth client_id read from the
  request auth context and recorded in the commit message.
- vault_reindex: on-demand KS index rebuild; force=True re-embeds
  everything. Serialized against the poller via _index_lock so a forced
  rebuild and a poll cycle never interleave.

Run:
    uv run python -m ks.server
"""

import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)

from . import config, db
from . import auth as ks_auth
from .auth import KSAuthProvider, consent_get, consent_post
from .embed import Embedder
from .indexer import run_index
from .writer import write_note

_embedder: Embedder | None = None

# Serializes index mutation between the poller thread and on-demand
# vault_reindex calls. Searches are unaffected (WAL readers).
_index_lock = threading.Lock()


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def _client_id() -> str:
    """Originating OAuth client for write attribution (Decision 29)."""
    token = get_access_token()
    if token is not None and getattr(token, "client_id", None):
        return token.client_id
    return "unauthenticated"


def _poll_loop() -> None:
    """Incremental reindex every POLL_INTERVAL_S, in a daemon thread.

    Started from main() so exactly one poller exists for the process
    lifetime, independent of MCP sessions (FastMCP runs its lifespan
    hook per-session under streamable-http — wrong lifecycle for this).

    An unchanged pass is stat + hash + one SQL read — milliseconds at
    vault scale. Embedding cost is only paid when a content hash moves.
    Errors (e.g. Ollama restarting) are logged and retried, never fatal.

    Also hosts expired-auth-row cleanup (M2) — one DELETE pass per cycle,
    no dedicated thread.
    """
    while True:
        try:
            with _index_lock:
                stats = run_index()
            if stats["indexed"] or stats["removed"]:
                print(
                    f"[{datetime.now():%H:%M:%S}] poller: "
                    f"indexed={stats['indexed']} removed={stats['removed']} "
                    f"chunks={stats['chunks']} elapsed={stats['elapsed_s']}s",
                    flush=True,
                )
        except Exception as exc:
            print(f"poller error (retrying next cycle): {exc}", file=sys.stderr, flush=True)
        try:
            ks_auth.cleanup_expired()
        except Exception as exc:
            print(f"auth cleanup error (retrying next cycle): {exc}", file=sys.stderr, flush=True)
        time.sleep(config.POLL_INTERVAL_S)


mcp = FastMCP(
    "knowledge-server",
    host=config.SERVER_HOST,
    port=config.SERVER_PORT,
    transport_security=TransportSecuritySettings(
        allowed_hosts=config.ALLOWED_HOSTS,
        allowed_origins=config.ALLOWED_ORIGINS,
    ),
    auth_server_provider=KSAuthProvider(),
    auth=AuthSettings(
        issuer_url=config.ISSUER_URL,
        resource_server_url=config.ISSUER_URL,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


@mcp.custom_route("/consent", methods=["GET"])
async def consent_page(request):
    return await consent_get(request)


@mcp.custom_route("/consent", methods=["POST"])
async def consent_submit(request):
    return await consent_post(request)


@mcp.tool()
def vault_search(
    query: str,
    k: int = 5,
    type: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
) -> dict:
    """Semantic search over the knowledge vault.

    Args:
        query: Natural-language search query.
        k: Maximum number of results (default 5).
        type: Optional frontmatter type filter (decision, runbook, note,
            glossary, reference, specification).
        status: Optional frontmatter status filter (e.g. active, superseded).
        include_archived: If true, superseded notes in archive/ are
            searched too. Default false — current knowledge only.

    Returns chunks ranked by cosine similarity, each with its source
    path, heading path, frontmatter metadata, and text.
    """
    qvec = _get_embedder().embed_query(query)
    conn = db.connect()
    try:
        results = db.search(
            conn,
            qvec,
            k=k,
            type_=type,
            status=status,
            include_archived=include_archived,
        )
    finally:
        conn.close()
    return {"results": [asdict(r) for r in results]}


@mcp.tool()
def vault_browse(
    path: str | None = None,
    type: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
) -> dict:
    """Browse the knowledge vault: list notes, or read one in full.

    Args:
        path: If given, return that note's full content (vault-relative
            path, e.g. "library/tech-projects/knowledge-server/example.md"). If omitted, return a
            listing of indexed notes with their frontmatter metadata.
        type: Optional type filter for the listing.
        status: Optional status filter for the listing.
        include_archived: Include archive/ notes in the listing.
    """
    if path is not None:
        root = config.VAULT_DIR.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target.suffix != ".md" or not target.is_file():
            return {"error": f"not a readable vault note: {path}"}
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    where: list[str] = []
    params: list = []
    if type:
        where.append("type = ?")
        params.append(type)
    if status:
        where.append("status = ?")
        params.append(status)
    if not include_archived:
        where.append("path NOT LIKE ?")
        params.append(config.ARCHIVE_PREFIX + "%")
    sql = "SELECT path, title, type, status, created, subject FROM files"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY path"

    conn = db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {
        "notes": [
            {
                "path": r[0],
                "title": r[1],
                "type": r[2],
                "status": r[3],
                "created": r[4],
                "subject": r[5],
            }
            for r in rows
        ]
    }


@mcp.tool()
def vault_write(path: str, content: str, dry_run: bool = False) -> dict:
    """Write a note to the knowledge vault (create or overwrite).

    Writes are constrained to the notes/ subtree; new subdirectories
    beneath it are created automatically. Content must be a complete
    Markdown note with Schema v1.0 YAML frontmatter (required fields:
    title, type, created, subject, relation, source, status). Invalid
    content or paths are rejected with structured errors. Successful
    writes are git-committed; the search index refreshes automatically
    within one poll cycle (~30 s).

    IMPORTANT: call with dry_run=true first and present the validation
    report (and diff, when overwriting) for human approval before
    committing a real write. Do not write to the vault unprompted.

    Args:
        path: Vault-relative destination, e.g. "notes/tech/my-note-v1.0.md".
            Lowercase kebab-case, .md extension, optional dotted-semver
            suffix.
        content: Full note content including the frontmatter block.
        dry_run: If true, validate and report (with diff on overwrite)
            without writing anything.
    """
    return write_note(path, content, dry_run=dry_run, client_id=_client_id())


@mcp.tool()
def vault_reindex(force: bool = False) -> dict:
    """Rebuild the Knowledge Server search index on demand.

    Normally unnecessary — a background poller refreshes the index every
    30 seconds. Use force=true only after embedder or chunking changes:
    it re-embeds the entire vault and may take ~30s or more.

    Args:
        force: If true, full re-embed of every note. If false,
            incremental pass (equivalent to one poller cycle).
    """
    with _index_lock:
        stats = run_index(force=force)
    return stats


def main() -> None:
    ks_auth.init_auth_db()
    threading.Thread(target=_poll_loop, name="vault-poller", daemon=True).start()
    print(f"poller started (interval {config.POLL_INTERVAL_S}s)", flush=True)
    print(
        f"Knowledge Server: http://{config.SERVER_HOST}:{config.SERVER_PORT}/mcp "
        f"(vault: {config.VAULT_DIR}, model: {config.EMBED_MODEL}, "
        f"issuer: {config.ISSUER_URL})",
        flush=True,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
