# Architecture Reference

- **Document type:** Canonical architecture specification
- **Status:** Finalized
- **Related:** [KNOWLEDGE-MODEL.md](KNOWLEDGE-MODEL.md) (rationale), [VAULT-SCHEMA.md](VAULT-SCHEMA.md) (note contract), [DEPLOYMENT.md](DEPLOYMENT.md) (ingress + auth)

---

## 1. Overview

The Knowledge Server exposes a governed Markdown knowledge vault to any [MCP](https://modelcontextprotocol.io) client over Streamable HTTP. It provides semantic search, browsing, validated writing, and index maintenance as four MCP tools. The server binds loopback only; public exposure is delegated to a reverse proxy / tunnel (see DEPLOYMENT.md).

Design stance: **minimal moving parts, local-first, rebuildable.** Embeddings are computed locally; similarity is computed in-process; the search index is a derived cache reconstructable from the vault at any time. No external vector database, no cloud embedding dependency.

---

## 2. Layered model

```
  Access layer      MCP tools over Streamable HTTP (FastMCP)
                    + OAuth 2.1 authorization server
  ───────────────────────────────────────────────────────────
  Index layer       SQLite (vectors + metadata), refreshed by a
                    30s background poller. DERIVED / REBUILDABLE.
  ───────────────────────────────────────────────────────────
  Storage layer     Markdown vault, git-versioned, Dublin Core
                    frontmatter. DURABLE SOURCE OF TRUTH.
```

The separation is load-bearing: the storage layer is the source of truth and survives everything above it; the index is a cache; the access layer is a swappable interface. Any layer above storage can be rebuilt or replaced without data loss.

---

## 3. Components

### 3.1 MCP server (`ks/server.py`)

A FastMCP application over `streamable-http`. Responsibilities:

- Registers the four tools (§4) and the OAuth consent routes.
- Configures transport security (host/origin allow-lists) so only expected hosts and origins are accepted.
- Starts the vault poller as a **daemon thread in `main()`** — deliberately *not* in the FastMCP lifespan hook, which runs per-session under streamable-http and would spawn a poller per connection. Exactly one poller exists for the process lifetime.
- Binds loopback (`127.0.0.1`) only.

### 3.2 Embedder (`ks/embed.py`)

Wraps a local [Ollama](https://ollama.com) instance over its HTTP API (via `httpx`) using the `mxbai-embed-large` model (1024-dim). The model card's instruction prefix is applied to **queries only, never documents** — an asymmetry the model requires for correct retrieval. The model is pinned resident (`keep_alive = -1`) to avoid cold-start latency.

### 3.3 Index + search (`ks/db.py`, `ks/indexer.py`, `ks/search.py`)

- **Indexer** walks the vault's index roots, splits notes into heading-aware chunks (bounded target size + overlap so no chunk is silently truncated at the model's context limit), embeds them, and upserts vectors + frontmatter into SQLite. Incremental by content hash: an unchanged pass is stat + hash + one SQL read.
- **Search** embeds the query and ranks chunks by cosine similarity (computed in-process with `numpy`), applying `type` / `status` / archive filters.
- **SQLite** is opened in WAL mode so search readers never block on the poller's writes.

### 3.4 Poller

Runs every 30s: an incremental reindex plus expired-auth-row cleanup. Errors (e.g. the embedding runtime restarting) are logged and retried, never fatal. Worst-case index staleness after a write is one poll interval — which satisfies "retrievable without a manual reindex."

### 3.5 Validator (`ks/validator.py`)

Pure, I/O-free enforcement of the note schema and path rules (see VAULT-SCHEMA.md §3.5). Two independent checks — `validate_path` and `validate_content` — merged into one result. Being pure makes it trivially testable and reusable.

### 3.6 Writer (`ks/writer.py`)

Composes the validator with filesystem-safe I/O:

1. Resolve the target against the **real** vault root and reject anything escaping it (defeats symlink and traversal escapes).
2. `dry_run=True`: validation report + create/overwrite status + unified diff. No mutation.
3. `dry_run=False`: atomic write (temp file + `os.replace` in the destination dir) → one git commit under a fixed author, with the originating client recorded in the commit body.

A module-level lock serializes write+commit so two concurrent tool calls cannot race on the git index.

### 3.7 Authorization server (`ks/auth.py`, `ks/setpass.py`)

An embedded OAuth 2.1 provider — see DEPLOYMENT.md §3.

---

## 4. The MCP tools

| Tool | Reads/Writes | Behavior |
|---|---|---|
| `vault_search` | read | Embeds query, cosine-ranks chunks, filters by `type`/`status`; excludes archived/superseded by default. |
| `vault_browse` | read | Lists indexed notes with metadata, or returns one note's full content (path validated against the vault root). |
| `vault_write` | write | Validated create/overwrite. `dry_run` returns report + diff without mutating. Git-committed on real write. |
| `vault_reindex` | write (index) | On-demand rebuild; `force=true` re-embeds everything. Serialized against the poller via a lock. |

---

## 5. Data flow

### 5.1 Read (search)

```
client → vault_search(query, filters)
       → embed query (Ollama)
       → cosine rank vs SQLite vectors (numpy, in-process)
       → apply type/status/archive filters
       → return ranked chunks + metadata + source paths
```

### 5.2 Write

```
client → vault_write(path, content, dry_run=true)
       → validate path + schema (validator)
       → report + diff, NO mutation
   (human approval)
client → vault_write(path, content)              [dry_run=false]
       → resolve target against real vault root
       → atomic write → git add + commit (fixed author, client in body)
       → poller re-indexes within ≤30s
```

---

## 6. Key design decisions

- **Loopback-only bind; ingress via proxy/tunnel.** The process never faces the public network directly; TLS termination and DDoS/edge concerns are the proxy's job.
- **Poller over filesystem-watch.** A poll loop is simpler and dependency-free; at vault scale an unchanged pass costs milliseconds, so the tradeoff (≤30s staleness) is negligible.
- **In-process similarity, no vector DB.** At this corpus scale, `numpy` over SQLite-stored vectors is sufficient and removes an entire class of operational dependency. This is a scale-appropriate choice, not a universal one (see §7).
- **Derived index.** The index is never authoritative; losing it is a rebuild, not data loss.
- **Enforce at write.** Correctness is a write-time invariant, not a read-time hope.

---

## 7. Constraints, assumptions, and failure modes

- **Scale assumption.** In-process cosine ranking and a poll-based indexer suit a small-to-moderate vault (order 10²–10³ notes). At much larger scale, the index layer would be swapped for a dedicated vector store — which the layered model permits without touching storage or access layers. This is the primary known scaling boundary.
- **Embedding runtime dependency.** If Ollama is down, search and write-indexing degrade; the poller logs and retries, and writes still succeed (the index catches up when the runtime returns). Writes are never blocked by index availability.
- **Single-writer serialization.** The write lock assumes one process. Horizontal scaling of the writer would require a different concurrency model; it is out of scope by design.
- **Model asymmetry.** The query-only instruction prefix is model-specific; swapping embedding models requires re-checking this and a full re-embed (`vault_reindex force=true`), since vector spaces are not comparable across models.
- **Staleness window.** Up to one poll interval between a write and its searchability. Acceptable by design; `vault_reindex` forces immediate refresh if ever needed.
