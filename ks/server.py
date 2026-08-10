"""Knowledge Server — FastMCP server over Streamable HTTP.

Exposes vault_search, vault_browse (M1), vault_write, vault_reindex
(M3, spec §5), vault_patch, vault_archive (M4, Decision 39) and runs
the in-process vault poller (build-log decision, 2026-07-10:
poll-based freshness, no watchdog dependency). Binds loopback only;
public ingress via a reverse proxy / tunnel. OAuth 2.1 AS embedded as
of M2 (ks/auth.py): DCR enabled, passphrase-gated /consent, JWT access
tokens.

M3 additions:
- vault_write: strict schema validation + library/** constraint
  (ks/validator.py), atomic write + per-write git commit (ks/writer.py,
  Decisions 27-29). dry_run flag = validation report + diff, no
  mutation (Decision 28). Originating OAuth client_id read from the
  request auth context and recorded in the commit message.
- vault_reindex: on-demand KS index rebuild; force=True re-embeds
  everything. Serialized against the poller via _index_lock so a forced
  rebuild and a poll cycle never interleave.

M4 additions (Decision 39):
- vault_patch: targeted string-replacement edit of an existing library/
  note, reusing vault_write's validation/commit path (ks/writer.py).
- vault_archive: library/ -> archive/ move via `git mv`, patching only
  the frontmatter `status` field, committed atomically.

Structured logging: every tool call is wrapped in @logged_tool
(ks/logging_config.py), which records a per-call JSONL audit line
(client, tool, outcome, duration, detail) independent of the git
commit trail written by ks/writer.py.

Run:
    uv run python -m ks.server
"""

import logging
import threading
import time
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from mcp.server.transport_security import TransportSecuritySettings
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
from .logging_config import configure_logging, get_client_id as _client_id, logged_tool
from .writer import archive_note, patch_note, write_note

configure_logging()

logger = logging.getLogger("ks.server")
logger.info("ks.startup: starting")

_embedder: Embedder | None = None

# Serializes index mutation between the poller thread and on-demand
# vault_reindex calls. Searches are unaffected (WAL readers).
_index_lock = threading.Lock()


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


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
                logger.info(
                    f"ks.poller: indexed={stats['indexed']} removed={stats['removed']} "
                    f"chunks={stats['chunks']} elapsed={stats['elapsed_s']}s"
                )
        except Exception as exc:
            logger.error(f"ks.poller: reindex error (retrying next cycle): {exc}")
        try:
            ks_auth.cleanup_expired()
        except Exception as exc:
            logger.error(f"ks.poller: auth cleanup error (retrying next cycle): {exc}")
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
@logged_tool
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
@logged_tool
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
@logged_tool
def vault_write(path: str, content: str, dry_run: bool = False) -> dict:
    """Write a note to the knowledge vault (create or overwrite).

    Writes are constrained to the library/ subtree; new subdirectories
    beneath it are created automatically. Content must be a complete
    Markdown note with Schema v1.1 YAML frontmatter. Invalid content or
    paths are rejected with structured errors. Successful writes are
    git-committed; the search index refreshes automatically within one
    poll cycle (~30 s).

    Required fields: title, type, created, subject, relation, source,
    status, identifier, creator, reviewed, reviewed_by, schema. This
    reference implementation validates but does not mint any of these
    on the caller's behalf -- the caller supplies a well-formed value
    for every field, including a canonical, lowercase, hyphenated
    UUIDv7 `identifier` and a `creator`/`reviewed_by` drawn from
    `AGENT_VOCAB` (ks/validator.py). A production deployment may choose
    to mint `identifier` and `schema` server-side instead; that is a
    valid extension of this contract, not part of the reference shape.

    IMPORTANT: call with dry_run=true first and present the validation
    report (and diff, when overwriting) for human approval before
    committing a real write. Do not write to the vault unprompted.

    Args:
        path: Vault-relative destination, e.g. "library/tech/my-note-v1.0.md".
            Lowercase kebab-case, .md extension, optional dotted-semver
            suffix.
        content: Full note content including the frontmatter block.
        dry_run: If true, validate and report (with diff on overwrite)
            without writing anything.
    """
    return write_note(path, content, dry_run=dry_run, client_id=_client_id())


@mcp.tool()
@logged_tool
def vault_patch(path: str, old_str: str, new_str: str, dry_run: bool = True) -> dict:
    """Apply a targeted string replacement to an existing vault note.

    Use for any modification to an existing document — appending, fixing
    a line, updating a section — unless the change touches most of the
    file, in which case use vault_write instead.

    old_str must match exactly once in the note's current content; zero
    or multiple matches is a hard error, no partial application. The
    patched document is re-validated against Schema v1.1 before any
    write — a patch that breaks frontmatter or empties the body is
    rejected before disk. Only files under library/ can be patched;
    archive/ notes are frozen.

    A patch that would change the note's `identifier` field is always
    rejected — identifiers are immutable once minted (Schema v1.1 §6.3).

    IMPORTANT: call with dry_run=true first (the default) and present
    the diff for human approval before committing a real patch.

    Args:
        path: Vault-relative path to an existing note under library/.
        old_str: Exact text to replace; must appear exactly once.
        new_str: Replacement text.
        dry_run: If true (default), validate and report a diff without
            writing anything.
    """
    return patch_note(path, old_str, new_str, dry_run=dry_run, client_id=_client_id())


@mcp.tool()
@logged_tool
def vault_archive(path: str, new_status: str = "superseded", dry_run: bool = True) -> dict:
    """Archive a vault note: move library/ -> archive/, updating its status.

    Resolves the mirrored archive/ destination by prefix substitution,
    patches only the frontmatter `status` field to new_status, and
    re-validates against Schema v1.1. Uses `git mv` so history is
    preserved; the content change and the move are committed as one
    atomic operation.

    Rejects: source not under library/, an archive/ target that already
    exists, or new_status outside the schema's status vocabulary
    (draft, active, superseded).

    IMPORTANT: call with dry_run=true first (the default) and present
    the diff for human approval before committing a real archive.

    Args:
        path: Vault-relative path to an existing note under library/.
        new_status: Frontmatter status to set on the archived note
            (default "superseded").
        dry_run: If true (default), validate and report a diff without
            writing anything.
    """
    return archive_note(path, new_status=new_status, dry_run=dry_run, client_id=_client_id())


@mcp.tool()
@logged_tool
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
    logger.info(f"ks.startup: poller started (interval {config.POLL_INTERVAL_S}s)")
    logger.info(
        f"ks.startup: serving on {config.SERVER_HOST}:{config.SERVER_PORT} "
        f"(vault: {config.VAULT_DIR}, model: {config.EMBED_MODEL}, "
        f"issuer: {config.ISSUER_URL})"
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
