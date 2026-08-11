# Deployment Guide

- **Document type:** Operational runbook
- **Status:** Finalized
- **Related:** [ARCHITECTURE.md](ARCHITECTURE.md), [README.md](../README.md)

> This repository is a **reference architecture**, not a turnkey appliance. It gives you a working server, a working authorization server, and the shape of a public deployment. The specifics of your host, TLS certificate, tunnel, and service supervisor are yours to supply. A competent systems architect (or a well-prompted architect persona reading this repo) should be able to fill in the environment-specific details.

---

## 1. Prerequisites

- A host you control (headless is fine). Local-first: no cloud services are required for the core to run.
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/).
- [**Ollama**](https://ollama.com/) with an embedding model pulled:
  ```bash
  ollama pull mxbai-embed-large
  ```
- **git** (the vault is a git repository; writes are commits).
- For public exposure only: a reverse proxy or tunnel and a domain you control.

---

## 2. Configuration

All deployment-specific values live in one file. Copy the example and edit the two marked lines:

```bash
cp ks/config.example.py ks/config.py
```

| Value | Edit? | Notes |
|---|---|---|
| `VAULT_DIR` | if not `~/knowledge-vault` | Must be a git-initialized directory. |
| `PUBLIC_HOSTNAME` | **yes** (public only) | Your public hostname; builds the OAuth issuer URL and host allow-list. |
| `GIT_AUTHOR_EMAIL` / `GIT_AUTHOR_NAME` | **yes** | Commit identity recorded on every vault write. |
| `EMBED_MODEL` / `EMBED_DIM` | only if changing model | Changing the model requires a full re-embed (`vault_reindex force=true`). |
| `SERVER_HOST` / `SERVER_PORT` | rarely | Defaults to loopback `127.0.0.1:8420`. |

`ks/config.py` is gitignored — your values never enter version control.

---

## 3. Authentication

The server embeds an **OAuth 2.1 authorization server** (`ks/auth.py`) with PKCE and Dynamic Client Registration (DCR). See [ARCHITECTURE.md §5.7](ARCHITECTURE.md#57-authorization-dcr-pkce-and-passphrase-consent) for the full registration → consent → token-exchange sequence diagram. Design:

- **Access tokens** are stateless HS256 JWTs — verified without a DB read on the hot path. Tradeoff: a live access token cannot be individually revoked; its blast radius is bounded by `ACCESS_TOKEN_TTL_S` (default 1h). Revocation applies to refresh tokens.
- **Refresh tokens and authorization codes** are opaque, rotating, and DB-stored (`ks-auth.db`).
- **Consent** is a passphrase-gated screen: a client's authorization request is only granted after the operator enters the vault passphrase. This is the human gate on which clients may connect.

### 3.1 Secret setup

Secrets live in `ks-secret.json` (mode 600, gitignored, never committed): a scrypt-hashed passphrase and the HS256 signing key.

```bash
uv run python -m ks.setpass            # set the passphrase (creates the JWT key)
uv run python -m ks.setpass --rotate-key   # rotate the signing key only
```

Rotating the key invalidates all live access tokens; connected clients recover automatically via refresh.

---

## 4. Public ingress (shape, not a copy of anyone's setup)

The server binds loopback only. To expose it:

```
   Internet ──TLS──▶ Reverse proxy / tunnel ──▶ 127.0.0.1:8420 (loopback)
                    (terminates TLS at PUBLIC_HOSTNAME)
```

- A **tunnel** (e.g. a Cloudflare Tunnel) or a reverse proxy (nginx/Caddy) terminates TLS at your `PUBLIC_HOSTNAME` and forwards to the loopback port. The server itself never binds a public interface.
- `PUBLIC_HOSTNAME` in `config.py` must match the hostname clients reach, because it builds the OAuth `issuer_url` and the transport-security allow-lists. A mismatch here is the most common first-deploy failure.
- The edge (proxy/tunnel) is the right place for rate-limiting the auth endpoints (`/consent`, `/register`) and for DDoS protection.

Supplying the certificate, DNS, and tunnel credentials is your responsibility — this repo does not ship anyone's infrastructure.

---

## 5. Running as a service

For development, run in the foreground:

```bash
uv run python -m ks.server
```

For a persistent deployment, supervise it with your platform's service manager (systemd on Linux, launchd on macOS, etc.). Requirements the supervisor must satisfy:

- Run as an **unprivileged user**, with `HOME` set explicitly (the server resolves paths from the home directory).
- Do **not** run package managers or the service as root against user-owned tool directories — it can leave root-owned files that break later upgrades.
- Capture stdout/stderr to log files for the startup banner and poller output.
- Restart on failure.

A `deploy/` directory template (e.g. a service-unit example) can be added per your platform; genericize any paths and usernames before committing.

---

## 6. Verification

After (re)starting:

1. **Process is up, not crash-looping.** Check your supervisor's status; you want a running state with a real PID, not a repeating spawn/exit.
2. **Startup banner logged.** `configure_logging()` (`ks/logging_config.py`) runs before anything else in `ks/server.py`, so even an import-time failure is logged rather than crash-looping silently. On a clean start, stderr shows:
   ```
   2026-08-10T09:00:00+0000 INFO ks.server: ks.startup: starting
   2026-08-10T09:00:00+0000 INFO ks.server: ks.startup: poller started (interval 30s)
   2026-08-10T09:00:00+0000 INFO ks.server: ks.startup: serving on 127.0.0.1:8420 (vault: …, model: …, issuer: …)
   ```
3. **Tool count sane.** The server registers exactly six tools (`vault_search`, `vault_browse`, `vault_write`, `vault_patch`, `vault_archive`, `vault_reindex`).
4. **Write path works.** A `vault_write` with `dry_run=true` to a path inside the writable subtree should return a validation report, not a write-root rejection.
5. **Tool-call log rotating.** Each tool call (any of the six) appends one JSONL line to the structured tool log (`ks/logging_config.py`'s rotating handler); confirm it's writing and not silently failing on a read-only path.

---

## 7. Operational notes

- **After editing any `ks/*.py`, restart the service.** Python loads modules at process start; on-disk edits do not affect a running process until it reloads.
- **Index staleness** after a write is ≤ one poll interval (30s). Use `vault_reindex` to force immediate refresh; `force=true` re-embeds everything (needed after an embedding-model change).
- **Never commit** `ks-secret.json`, `ks-auth.db`, `ks-index.db`, or `ks/config.py` — all are gitignored; verify before any first push.
- **Client tool discovery:** some MCP clients cache tool schemas per connection. After adding or renaming a tool, reconnect the client (or start a new session) so it re-reads the schema. A pure code-logic change behind an unchanged tool signature needs only a server restart.
