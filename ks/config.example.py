"""
Central configuration for the Knowledge Server index.

All paths and tunables live here. Nothing else in the package hardcodes
a path, model name, or size threshold.

DEPLOYMENT: copy this file to `config.py` and edit the values marked
with `# EDIT` for your environment. `config.py` is gitignored so your
deployment-specific values never enter version control.
"""
from pathlib import Path

# --- Locations ---
VAULT_DIR = Path.home() / "knowledge-vault"
DB_PATH = Path.home() / "knowledge-server" / "ks-index.db"

# Roots inside the vault that get indexed. archive/ IS indexed so that
# superseded notes remain retrievable on explicit request (spec §12.3);
# search excludes it by default via the path prefix.
INDEX_ROOTS = ["library", "archive"]
EXTRA_FILES = ["_KNOWLEDGE_MAP.md"]
ARCHIVE_PREFIX = "archive/"

# --- Embedding ---
OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "mxbai-embed-large"
EMBED_DIM = 1024
# mxbai model-card instruction prefix: applied to QUERIES only,
# never to documents.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
# Pin the model resident in Ollama (~670 MB RAM), no cold starts.
KEEP_ALIVE = -1
EMBED_BATCH_SIZE = 16
EMBED_TIMEOUT_S = 120.0

# --- MCP server ---
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8420
POLL_INTERVAL_S = 30

# --- Chunking ---
# mxbai-embed-large context window is 512 tokens. Target ~400 tokens
# (~4 chars/token heuristic) so no chunk is silently truncated.
CHUNK_TARGET_CHARS = 1600
CHUNK_OVERLAP_CHARS = 200

# --- Public ingress ---
# EDIT: the public hostname you front the server with (e.g. via a
# Cloudflare Tunnel or reverse proxy). Used to build the OAuth issuer
# URL and the transport-security allow lists below.
PUBLIC_HOSTNAME = "ks.example.com"
ISSUER_URL = f"https://{PUBLIC_HOSTNAME}"
ALLOWED_HOSTS = [
    PUBLIC_HOSTNAME,
    f"{PUBLIC_HOSTNAME}:443",
    "127.0.0.1:8420",
    "localhost:8420",
]
ALLOWED_ORIGINS = [ISSUER_URL]

# --- Git commit identity (vault_write) ---
# EDIT: the author recorded on every vault_write commit. Set via git -c
# flags so the vault repo's own git config is never consulted; the
# originating OAuth client is recorded in the commit message body.
GIT_AUTHOR_NAME = "Knowledge Server"
GIT_AUTHOR_EMAIL = "ks@example.com"

# --- Auth (M2, Decisions 14/15, Forks A1/B2/C2) ---
AUTH_DB_PATH = Path.home() / "knowledge-server" / "ks-auth.db"
SECRET_PATH = Path.home() / "knowledge-server" / "ks-secret.json"
JWT_AUDIENCE = "ks"
ACCESS_TOKEN_TTL_S = 3600
REFRESH_TOKEN_TTL_S = 30 * 86400
AUTH_CODE_TTL_S = 300
PENDING_AUTH_TTL_S = 600

# --- Review staleness (Schema v1.1, vault spec §7.2) ---
# Default review interval in days, keyed by note `type`. None = never goes
# stale. Overridden per-note by the optional `review_interval` frontmatter
# field (three states: absent -> this default; int -> override; explicit
# null -> never expires regardless of type).
#
# `decision` and `glossary` are permanently true statements about the past
# or about definitions -- putting them on a review treadmill generates
# noise that trains the operator to ignore review flags, which defeats the
# mechanism. `journal` is a dated first-person record and is likewise
# permanently true the moment it's written.
REVIEW_INTERVALS = {
    "runbook": 90,
    "specification": 90,
    "reference": 180,
    "note": 180,
    "decision": None,
    "glossary": None,
    "journal": None,
}


def resolve_review_interval(
    note_type: str, review_interval_present: bool, review_interval_value: int | None
) -> int | None:
    """Three-state resolution (vault spec §7.2): explicit null and absent
    both leave `review_interval_value` unset, so the *_present flag is
    what tells them apart -- present-and-None means "never expires
    regardless of type," absent means "use the type default."

    A type missing from REVIEW_INTERVALS resolves to None (never stale)
    rather than raising -- callers should log that as a config gap, since
    a new TYPE_VOCAB entry with no interval entry is a defect but must
    not break indexing.
    """
    if review_interval_present:
        return review_interval_value
    return REVIEW_INTERVALS.get(note_type)
