"""Vault write path (M3, Decisions 27-29).

Composes ks.validator (pure schema/path rules) with the filesystem- and
git-level concerns that need real I/O:

  1. Resolve the requested path against the actual vault root and reject
     anything that escapes it (symlink defense -- resolve() follows
     existing symlinks in every component, so a link inside library/
     pointing outside the vault fails is_relative_to()).
  2. dry_run=True: full validation report, create/overwrite status, and
     a unified diff when overwriting. No mutation of any kind.
  3. dry_run=False: mkdir -p, atomic write (temp file + os.replace in
     the destination directory), then one git commit (Decision 29).

Commit identity: a fixed author (config.GIT_AUTHOR_NAME /
config.GIT_AUTHOR_EMAIL) is set via -c flags so the vault repo's own git
config is never consulted; the originating OAuth client is recorded in
the commit message body for harness attribution.

Concurrency: a module-level lock serializes write+commit. Two
simultaneous tool calls would otherwise race on the git index. At this
scale a plain lock is correct and sufficient.

The KS index is NOT updated here -- the 30 s poller is the single
index-update path (build-log freshness decision). Worst-case staleness
is one poll interval, which satisfies "retrievable without manual
reindex" (spec §11 M3 exit criterion).
"""

import difflib
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from . import config
from .validator import validate

_write_lock = threading.Lock()


def _resolve_target(rel_path: str) -> Path | None:
    """Resolve rel_path inside the vault; None if it escapes the root."""
    root = config.VAULT_DIR.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        return None
    return target


def _git(*args: str) -> subprocess.CompletedProcess:
    cmd = [
        "git",
        "-C",
        str(config.VAULT_DIR),
        "-c",
        f"user.name={config.GIT_AUTHOR_NAME}",
        "-c",
        f"user.email={config.GIT_AUTHOR_EMAIL}",
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def write_note(
    path: str,
    content: str,
    dry_run: bool = False,
    client_id: str = "unknown",
) -> dict:
    """Validate and (unless dry_run) write + commit a vault note.

    Returns a dict in one of three shapes:
      invalid:  {"ok": False, "errors": [...]}
      dry run:  {"ok": True, "dry_run": True, "action": "create"|"overwrite",
                 "path": ..., "diff": ...}
      written:  {"ok": True, "dry_run": False, "action": ...,
                 "path": ..., "commit": "<short-hash>"}
    """
    result = validate(path, content)
    if not result.ok:
        return {"ok": False, "errors": result.errors}

    target = _resolve_target(path)
    if target is None:
        return {
            "ok": False,
            "errors": [
                {
                    "field": "path",
                    "rule": "vault-escape",
                    "message": "resolved path escapes the vault root (symlink or traversal)",
                }
            ],
        }
    if target.exists() and not target.is_file():
        return {
            "ok": False,
            "errors": [
                {
                    "field": "path",
                    "rule": "not-a-file",
                    "message": "path exists and is not a regular file",
                }
            ],
        }

    exists = target.is_file()
    action = "overwrite" if exists else "create"

    diff_text = None
    if exists:
        old = target.read_text(encoding="utf-8")
        diff_lines = difflib.unified_diff(
            old.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        diff_text = "".join(diff_lines)

    if dry_run:
        out = {"ok": True, "dry_run": True, "action": action, "path": path}
        if diff_text is not None:
            out["diff"] = diff_text if diff_text else "(no content change)"
        return out

    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=".ks-write-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

        add = _git("add", "--", path)
        if add.returncode != 0:
            return {
                "ok": False,
                "errors": [
                    {"field": "git", "rule": "add", "message": add.stderr.strip()}
                ],
            }
        message = (
            f"vault_write: {action} {path}\n\n"
            f"client: {client_id}\n"
            f"via: knowledge-server vault_write"
        )
        commit = _git("commit", "-m", message, "--", path)
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return {
                    "ok": True,
                    "dry_run": False,
                    "action": "no-change",
                    "path": path,
                    "commit": None,
                }
            return {
                "ok": False,
                "errors": [
                    {"field": "git", "rule": "commit", "message": commit.stderr.strip()}
                ],
            }
        rev = _git("rev-parse", "--short", "HEAD")
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else "unknown"

    return {
        "ok": True,
        "dry_run": False,
        "action": action,
        "path": path,
        "commit": commit_hash,
    }
