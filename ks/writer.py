"""Vault write path (M3, Decisions 27-29; M4, Decision 39 vault_patch/vault_archive).

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

patch_note / archive_note (Decision 39) reuse _resolve_target, _git, and
_write_lock rather than introducing any new write infrastructure. Both
re-validate the resulting content with the same validator as write_note
-- a patch or archive that would produce a schema-invalid note is
rejected before anything touches disk, same as a rejected write.
"""

import difflib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from . import config
from .validator import STATUS_VOCAB, WRITE_ROOT, validate, validate_content, validate_path
from .vault import parse_frontmatter

_write_lock = threading.Lock()


class PatchError(Exception):
    """old_str did not match exactly once, or no frontmatter status field found."""


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


def _patch_content(content: str, old_str: str, new_str: str) -> str:
    """Replace old_str with new_str in content. old_str must match exactly once.

    Fails closed: zero or 2+ matches raise PatchError rather than guessing,
    since a partial or wrong application would be worse than no change.
    """
    count = content.count(old_str)
    if count == 0:
        raise PatchError("old_str not found in current content")
    if count > 1:
        raise PatchError(f"old_str matches {count} times, must match exactly once")
    return content.replace(old_str, new_str, 1)


_STATUS_LINE_RE = re.compile(r"^status:\s*(.+)$", re.MULTILINE)


def _frontmatter_bounds(content: str) -> tuple[int, int] | None:
    """Char offsets (start, end) of the '---'-delimited frontmatter block."""
    if not content.startswith("---\n"):
        return None
    close = content.find("\n---", 4)
    if close == -1:
        return None
    return 0, close + len("\n---")


def _set_status(content: str, new_status: str) -> str:
    """Rewrite only the frontmatter `status` line, leaving everything else untouched."""
    bounds = _frontmatter_bounds(content)
    if bounds is None:
        raise PatchError("content has no '---'-delimited frontmatter block")
    start, end = bounds
    header = content[start:end]
    match = _STATUS_LINE_RE.search(header)
    if match is None:
        raise PatchError("no 'status:' field found in frontmatter")
    new_header = _patch_content(header, match.group(0), f"status: {new_status}")
    return content[:start] + new_header + content[end:]


def patch_note(
    path: str,
    old_str: str,
    new_str: str,
    dry_run: bool = True,
    client_id: str = "unknown",
) -> dict:
    """Apply a targeted string replacement to an existing library/ note.

    Returns a dict in one of three shapes (same contract as write_note):
      invalid:  {"ok": False, "errors": [...]}
      dry run:  {"ok": True, "dry_run": True, "action": "patch",
                 "path": ..., "diff": ...}
      written:  {"ok": True, "dry_run": False, "action": "patch",
                 "path": ..., "commit": "<short-hash>"}
    """
    pr = validate_path(path)
    if not pr.ok:
        return {"ok": False, "errors": pr.errors}

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
    if not target.is_file():
        return {
            "ok": False,
            "errors": [
                {"field": "path", "rule": "not-found", "message": f"no such file: {path}"}
            ],
        }

    old_content = target.read_text(encoding="utf-8")
    try:
        new_content = _patch_content(old_content, old_str, new_str)
    except PatchError as exc:
        return {
            "ok": False,
            "errors": [{"field": "old_str", "rule": "exact-one-match", "message": str(exc)}],
        }

    cr = validate_content(new_content)
    if not cr.ok:
        return {"ok": False, "errors": cr.errors}

    old_fm, _ = parse_frontmatter(old_content)
    if old_fm.get("identifier") != cr.frontmatter.get("identifier"):
        return {
            "ok": False,
            "errors": [
                {
                    "field": "identifier",
                    "rule": "immutable",
                    "message": "a patch must not change a note's identifier (spec §6.3)",
                }
            ],
        }

    diff_text = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "action": "patch",
            "path": path,
            "diff": diff_text if diff_text else "(no content change)",
        }

    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".ks-write-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
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
                "errors": [{"field": "git", "rule": "add", "message": add.stderr.strip()}],
            }
        message = (
            f"vault_patch: {path}\n\n"
            f"client: {client_id}\n"
            f"via: knowledge-server vault_patch"
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
                "errors": [{"field": "git", "rule": "commit", "message": commit.stderr.strip()}],
            }
        rev = _git("rev-parse", "--short", "HEAD")
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else "unknown"

    return {
        "ok": True,
        "dry_run": False,
        "action": "patch",
        "path": path,
        "commit": commit_hash,
    }


def archive_note(
    path: str,
    new_status: str = "superseded",
    dry_run: bool = True,
    client_id: str = "unknown",
) -> dict:
    """Move a library/ note to its mirrored archive/ path, setting status.

    Returns a dict in one of three shapes (same contract as write_note):
      invalid:  {"ok": False, "errors": [...]}
      dry run:  {"ok": True, "dry_run": True, "action": "archive",
                 "path": ..., "archive_path": ..., "diff": ...}
      written:  {"ok": True, "dry_run": False, "action": "archive",
                 "path": ..., "archive_path": ..., "commit": "<short-hash>"}
    """
    pr = validate_path(path)
    if not pr.ok:
        return {"ok": False, "errors": pr.errors}

    if new_status not in STATUS_VOCAB:
        return {
            "ok": False,
            "errors": [
                {
                    "field": "status",
                    "rule": "vocabulary",
                    "message": f"status '{new_status}' not in {sorted(STATUS_VOCAB)}",
                }
            ],
        }

    archive_path = config.ARCHIVE_PREFIX + path[len(WRITE_ROOT) + 1 :]

    src = _resolve_target(path)
    dst = _resolve_target(archive_path)
    if src is None or dst is None:
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
    if not src.is_file():
        return {
            "ok": False,
            "errors": [
                {"field": "path", "rule": "not-found", "message": f"no such file: {path}"}
            ],
        }
    if dst.exists():
        return {
            "ok": False,
            "errors": [
                {
                    "field": "path",
                    "rule": "target-exists",
                    "message": f"archive target already exists: {archive_path}",
                }
            ],
        }

    old_content = src.read_text(encoding="utf-8")
    try:
        new_content = _set_status(old_content, new_status)
    except PatchError as exc:
        return {
            "ok": False,
            "errors": [{"field": "status", "rule": "frontmatter-status", "message": str(exc)}],
        }

    cr = validate_content(new_content)
    if not cr.ok:
        return {"ok": False, "errors": cr.errors}

    diff_text = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{archive_path}",
        )
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "action": "archive",
            "path": path,
            "archive_path": archive_path,
            "diff": diff_text if diff_text else "(no content change)",
        }

    with _write_lock:
        fd, tmp_name = tempfile.mkstemp(dir=src.parent, prefix=".ks-write-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.replace(tmp_name, src)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

        dst.parent.mkdir(parents=True, exist_ok=True)
        mv = _git("mv", "--", path, archive_path)
        if mv.returncode != 0:
            return {
                "ok": False,
                "errors": [{"field": "git", "rule": "mv", "message": mv.stderr.strip()}],
            }
        message = (
            f"vault_archive: {path} -> {archive_path}\n\n"
            f"client: {client_id}\n"
            f"via: knowledge-server vault_archive"
        )
        commit = _git("commit", "-m", message, "--", path, archive_path)
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return {
                    "ok": True,
                    "dry_run": False,
                    "action": "no-change",
                    "path": path,
                    "archive_path": archive_path,
                    "commit": None,
                }
            return {
                "ok": False,
                "errors": [{"field": "git", "rule": "commit", "message": commit.stderr.strip()}],
            }
        rev = _git("rev-parse", "--short", "HEAD")
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else "unknown"

    return {
        "ok": True,
        "dry_run": False,
        "action": "archive",
        "path": path,
        "archive_path": archive_path,
        "commit": commit_hash,
    }
