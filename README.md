# Knowledge Server

**A harness-agnostic semantic knowledge layer.** A governed Markdown vault, exposed to any AI agent over the [Model Context Protocol (MCP)](https://modelcontextprotocol.io), with correctness enforced at write time rather than hoped for at read time.

The point is not the code. The code is deliberately small and legible. The point is a discipline: a way of structuring machine-maintained knowledge so that an AI system does not merely *store and recall* text, but operates over a corpus whose **meaning, provenance, and lifecycle are explicit and machine-checkable.**

---

## The idea in one paragraph

Most "give your AI a memory" systems are a vector database and a similarity search. That gives you *recall* -- the ability to fetch text that looks like your query. It does not give you *comprehension* — the ability to know which document is authoritative, which has been superseded, what kind of artifact each one is, or how they relate. Recall without governance rots: the index fills with contradictory, stale, and duplicate material, and retrieval quality degrades precisely as the corpus grows. This project treats that as an **architecture problem, not a model-capability problem.** It puts a thin, enforced **semantic layer** — a controlled vocabulary, a metadata ontology, and a lifecycle model — *above* the vector index, so that retrieval is grounded in declared meaning, not just geometric proximity.

For the full argument, see **[docs/KNOWLEDGE-MODEL.md](docs/KNOWLEDGE-MODEL.md)** — that document is the heart of this repository.

---

## Why this exists

Naive retrieval-augmented generation (RAG) has a well-known failure mode often called **context rot**: as a knowledge base accumulates, semantic search increasingly returns material that is *similar* but *wrong for the moment* -- an obsolete runbook, a decision that was later reversed, an early draft alongside its final version. Because a bare embedding index has no concept of authority or time, it cannot prefer the current truth over a well-phrased ghost.

The usual reflex is to reach for a more capable model. That does not fix it. Inconsistent structure across documents is not something a larger model repairs; it is a gap in the *system's* design. The corrective is to make structure and status **first-class, enforced properties of every document** — and to enforce them at the moment of writing, so that malformed or unclassifiable knowledge can never enter the corpus in the first place.

---

## Core thesis: governance above vector RAG

Two layers, cooperating:

1. **The vector layer (recall).** Documents are chunked and embedded; queries retrieve by cosine similarity. This is a geometric proxy for *meaning* — it finds text that is semantically near the query. Necessary, but insufficient.

2. **The governed semantic layer (comprehension).** Every document carries typed, validated metadata drawn from an established vocabulary. This layer answers the questions the vector layer cannot:
   - *What kind of thing is this?* — a controlled `type` (`decision`, `runbook`, `reference`, …).
   - *Is it still true?* — a controlled `status` lifecycle (`draft` → `active` → `superseded`).
   - *Where did it come from, and what does it relate to?* — `source` and `relation` provenance fields.

Search defaults to **current knowledge only** — superseded material is retained (for audit and explicit recall) but excluded from ordinary retrieval. That single governed field, `status`, is the structural answer to context rot.

---

## Standards it leans on

This project deliberately reuses established vocabularies rather than inventing bespoke ones, so the vault stays interoperable and has a clean upgrade path toward formal linked data.

- **[Dublin Core](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/) metadata terms** — `title`, `type`, `subject`, `date`, `source`, `relation`. An established, RDF-compatible vocabulary. Using its field names means any note can later be projected into JSON-LD / YAML-LD **without a field-renaming migration** if a formal knowledge-graph layer is ever added.
- **Controlled vocabularies** — the `type` and `status` fields draw from small, fixed, queryable sets rather than free text. An unconstrained field defeats the purpose of having one.
- **Model Context Protocol (MCP)** — the vault is exposed as MCP tools, so it is *harness-agnostic*: any current or future agent framework connects the same way, over the same wire protocol, with no bespoke integration.
- **Semantic chunking** — documents are split on heading structure with bounded overlap, so retrieved chunks are coherent units rather than arbitrary character windows.

See **[docs/VAULT-SCHEMA.md](docs/VAULT-SCHEMA.md)** for the exact field contract.

---

## Architecture at a glance

```
                          ┌──────────────────────────────┐
   any MCP client         │        Knowledge Server       │
   (AI agent, IDE,        │        (FastMCP / HTTP)        │
    desktop assistant) ───┤                                │
        OAuth 2.1         │  tools:                        │
        + MCP over HTTP   │    vault_search   vault_write  │
                          │    vault_browse   vault_reindex│
                          └───────┬───────────────┬────────┘
                                  │               │
                    read (embed + │               │ write (validate → commit)
                     cosine rank) │               │
                          ┌───────▼───────┐  ┌────▼─────────┐
                          │ SQLite index  │  │  Validator    │
                          │ (vectors,     │  │  Schema v1.0  │
                          │  metadata)    │  │  enforcement  │
                          └───────▲───────┘  └────┬─────────┘
                                  │               │
                       30s poller │               │ atomic write + git commit
                                  │               ▼
                          ┌───────┴───────────────────────────┐
                          │        Markdown Knowledge Vault     │
                          │  library/<category>/<subject>/*.md  │
                          │  archive/…  (superseded, retained)  │
                          │  + Dublin Core YAML frontmatter      │
                          │  + git history (versioning/provenance)│
                          └────────────────────────────────────┘

  Embeddings: local Ollama (mxbai-embed-large). No data leaves the host.
  Similarity: computed in-process (numpy). No external vector database.
```

Two independent paths meet at the vault:

- **Read path** — a query is embedded, ranked by cosine similarity against the SQLite index, and filtered by governed metadata. A background poller keeps the index fresh (~30s worst-case staleness); no manual reindex is required.
- **Write path** — every write is validated against the schema *before* anything touches disk, then written atomically and recorded as a single git commit. Invalid content is rejected with a structured, machine-actionable error. See below.

---

## Controls on write: correctness by construction

The vault cannot be corrupted by careless or malformed writes, because the write tool refuses them. `vault_write` composes two pure validators (path rules + content/schema rules) with filesystem-safe I/O:

- **Schema enforcement.** Content must begin with a well-formed YAML frontmatter block containing every required field (`title`, `type`, `created`, `subject`, `relation`, `source`, `status`). `type` and `status` must be members of their controlled vocabularies; `created` must be an ISO date; `subject` and `relation` must be string lists; the body must be non-empty.
- **Path discipline.** Writes are constrained to a designated writable subtree. Filenames and directory segments must be lowercase kebab-case; an optional dotted-semver suffix (e.g. `-v1.2`) supports explicit document versioning. Absolute paths, `..` traversal, and (via the writer's real-root resolution) symlink escapes are all rejected.
- **Two-phase, human-in-the-loop writes.** Callers are expected to invoke `dry_run=true` first: this returns the full validation report and — when overwriting — a unified diff, **without mutating anything.** A real write happens only after explicit approval. Agents do not write to the vault unprompted.
- **Versioned, attributed history.** Every successful write is a single git commit with a fixed author identity, and the originating client is recorded in the commit message body. The corpus is fully auditable; nothing is lost, only superseded.

The result is *correctness by construction*: the only way to get a document into the vault is to produce one that satisfies the ontology. Structure is not a convention the model is asked to follow — it is an invariant the system enforces.

---

## The MCP tools

| Tool | Purpose |
|---|---|
| `vault_search` | Semantic search over current knowledge; filterable by `type`/`status`; superseded material excluded by default. |
| `vault_browse` | List indexed notes with their metadata, or read one note in full. |
| `vault_write` | Validated, versioned create/overwrite. `dry_run` returns a report + diff without mutating. |
| `vault_reindex` | On-demand index rebuild (rarely needed; the poller handles freshness). |

---

## Quickstart

> This repository is a **reference architecture**, not a turnkey appliance. It assumes a reader who is comfortable operating their own host, embedding runtime, and (for public exposure) reverse proxy / tunnel and identity setup. A well-prompted systems-architect persona should be able to read this repo and fill in the environment-specific details.

Prerequisites: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com/) with an embedding model pulled.

```bash
# 1. Embedding runtime
ollama pull mxbai-embed-large

# 2. Configuration — copy the example and edit the two marked values
cp ks/config.example.py ks/config.py
#    edit PUBLIC_HOSTNAME and GIT_AUTHOR_EMAIL

# 3. Auth secret (scrypt passphrase + JWT signing key, mode 600)
uv run python -m ks.setpass

# 4. Point VAULT_DIR at a git-initialized Markdown vault, then run
uv run python -m ks.server
```

The server binds loopback only. See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** for the public-ingress shape (tunnel + OAuth).

---

## Deployment shape (public ingress)

For exposure beyond localhost, the intended shape is:

- **Reverse proxy / tunnel** (e.g. a Cloudflare Tunnel) terminating TLS at a public hostname and forwarding to the loopback-bound server. The server never binds a public interface directly.
- **OAuth 2.1** (PKCE + Dynamic Client Registration) as the authorization server, embedded in the process. Access tokens are stateless HS256 JWTs; refresh tokens and authorization codes are opaque and DB-stored. A passphrase-gated consent screen gates client authorization.

The specifics of *your* hostname, certificate, and tunnel are yours to supply — this repo gives the shape and the working auth server, not a copy of anyone's infrastructure.

---

## Roadmap: from vault to knowledge graph

The metadata model is chosen so that the natural next step is *additive*, not a rewrite. Because the frontmatter already uses Dublin Core (RDF-compatible) field names, each note can be projected into JSON-LD / YAML-LD and lifted into a formal **knowledge graph** — typed entities (nodes) and typed relationships (edges) governed by a single **ontology**, reusing `schema.org` vocabulary where it already covers a concept and extending only where it does not. That unlocks multi-hop relational queries a document search cannot answer directly. This is recorded as a design direction, not a current commitment; the schema is verified compatible with it before either is built further. See **[docs/ROADMAP.md](docs/ROADMAP.md)** and **[docs/KNOWLEDGE-MODEL.md](docs/KNOWLEDGE-MODEL.md) §8**.

---

## Repository layout

```
knowledge-server/
├── README.md
├── pyproject.toml            # 5 deps: httpx, mcp, numpy, pyjwt, pyyaml
├── .gitignore                # secrets, live config, databases — never committed
├── docs/
│   ├── KNOWLEDGE-MODEL.md     # ★ the theory: why governance beats bare RAG
│   ├── VAULT-SCHEMA.md        # the Dublin Core frontmatter contract
│   ├── ARCHITECTURE.md        # component + data-flow reference
│   ├── DEPLOYMENT.md          # tunnel + OAuth shape
│   └── ROADMAP.md             # knowledge-graph expansion
└── ks/
    ├── config.example.py      # copy → config.py, edit two lines
    ├── server.py              # FastMCP entry; the four tools; the poller
    ├── validator.py           # pure Schema v1.0 enforcement
    ├── writer.py              # atomic write + git commit
    ├── auth.py                # OAuth 2.1 / PKCE / DCR authorization server
    ├── setpass.py             # passphrase + JWT key management
    └── …                      # db, embed, indexer, search, vault
```

---

## License

MIT. See [LICENSE](LICENSE).
