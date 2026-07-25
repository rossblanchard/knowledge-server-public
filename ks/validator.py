"""Strict Schema v1.0 validation for vault writes (M3, Decisions 27-28).

Two independent checks, both pure (no filesystem access, no I/O):

  validate_path(path)        -- vault-relative path rules (library/** root,
                                .md extension, kebab-case filename)
  validate_content(content)  -- frontmatter presence + schema rules +
                                non-empty body

The writer (ks/writer.py) composes these with its own filesystem-level
checks (symlink/traversal resolution against the real vault root), which
require I/O and therefore live there, not here.

Error contract: every failure is a dict {"field", "rule", "message"} --
machine-actionable for agent callers (spec §11 M3 exit criterion:
"invalid write rejected with structured error").

Schema source: Knowledge-Vault-Schema-v1.0.md §3. The `specification`
type is included per spec open item §14.5 (ratified in-session
2026-07-16; existing vault specs would fail validation without it).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath

import yaml

# --- Schema v1.0 constants ---

REQUIRED_FIELDS = ("title", "type", "created", "subject", "relation", "source", "status")

TYPE_VOCAB = frozenset(
    {"decision", "runbook", "note", "glossary", "reference", "specification"}
)

STATUS_VOCAB = frozenset({"draft", "active", "superseded"})

# Writable subtree root (Decision 27). Everything else in the vault
# (archive/, _KNOWLEDGE_MAP.md, .githooks/) is owner-manual territory.
WRITE_ROOT = "library"

# lowercase kebab-case tokens, optional dotted-semver suffix, .md only.
# Matches: mcp-server-build-log-v0.3.md, gpu-notes.md, scotch-library.md
# Rejects: MyNote.md, notes_2026.md, draft..md, v0.3.md.bak
FILENAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(-v\d+\.\d+(\.\d+)?)?\.md$")

# Directory segments: kebab-case, no leading dot, no uppercase.
DIRNAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

FRONTMATTER_DELIM = "---"


@dataclass
class ValidationResult:
    ok: bool
    errors: list[dict] = field(default_factory=list)
    frontmatter: dict | None = None  # parsed metadata on success


def _err(field_name: str, rule: str, message: str) -> dict:
    return {"field": field_name, "rule": rule, "message": message}


# --- Path validation ---

def validate_path(path: str) -> ValidationResult:
    """Validate a vault-relative write path against Decision 27 rules.

    Pure string checks only. The writer must additionally resolve the
    joined path against the real vault root to defeat symlink escapes.
    """
    errors: list[dict] = []
    p = PurePosixPath(path)

    if p.is_absolute():
        errors.append(_err("path", "relative", "path must be vault-relative, not absolute"))
    if ".." in p.parts:
        errors.append(_err("path", "no-traversal", "path must not contain '..'"))
    if not errors:
        if not p.parts or p.parts[0] != WRITE_ROOT:
            errors.append(
                _err("path", "write-root", f"writes are constrained to '{WRITE_ROOT}/'")
            )
        if len(p.parts) < 2:
            errors.append(
                _err("path", "not-a-file", f"path must name a file under '{WRITE_ROOT}/'")
            )
    if not errors:
        for seg in p.parts[1:-1]:
            if not DIRNAME_RE.match(seg):
                errors.append(
                    _err(
                        "path",
                        "dirname-convention",
                        f"directory segment '{seg}' must be lowercase kebab-case",
                    )
                )
        if not FILENAME_RE.match(p.name):
            errors.append(
                _err(
                    "path",
                    "filename-convention",
                    f"filename '{p.name}' must be lowercase kebab-case .md "
                    "(optional dotted-semver suffix, e.g. some-note-v1.2.md)",
                )
            )
    return ValidationResult(ok=not errors, errors=errors)


# --- Content validation ---

def _split_frontmatter(content: str) -> tuple[str | None, str | None]:
    """Return (yaml_text, body) or (None, None) if the block is malformed."""
    if not content.startswith(FRONTMATTER_DELIM + "\n"):
        return None, None
    rest = content[len(FRONTMATTER_DELIM) + 1 :]
    end = rest.find("\n" + FRONTMATTER_DELIM)
    if end == -1:
        return None, None
    yaml_text = rest[:end]
    after = rest[end + 1 + len(FRONTMATTER_DELIM) :]
    body = after.lstrip("\n")
    return yaml_text, body


def _check_created(value) -> str | None:
    """Return an error message, or None if valid ISO date."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return None
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
            return None
        except ValueError:
            return f"'{value}' is not a valid ISO date (YYYY-MM-DD)"
    return f"expected ISO date, got {type(value).__name__}"


def _check_str_list(value, field_name: str) -> str | None:
    if not isinstance(value, list):
        return f"{field_name} must be a list"
    if any(not isinstance(x, str) or not x.strip() for x in value):
        return f"{field_name} must contain only non-empty strings"
    return None


def validate_content(content: str) -> ValidationResult:
    """Validate note content against Schema v1.0 §3."""
    errors: list[dict] = []

    yaml_text, body = _split_frontmatter(content)
    if yaml_text is None:
        return ValidationResult(
            ok=False,
            errors=[
                _err(
                    "frontmatter",
                    "block-present",
                    "content must begin with a '---'-delimited YAML frontmatter block",
                )
            ],
        )

    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return ValidationResult(
            ok=False,
            errors=[_err("frontmatter", "yaml-parse", f"frontmatter is not valid YAML: {exc}")],
        )
    if not isinstance(fm, dict):
        return ValidationResult(
            ok=False,
            errors=[_err("frontmatter", "yaml-mapping", "frontmatter must be a YAML mapping")],
        )

    for name in REQUIRED_FIELDS:
        if name not in fm:
            errors.append(_err(name, "required", f"missing required field '{name}'"))
    if errors:
        return ValidationResult(ok=False, errors=errors, frontmatter=fm)

    if not isinstance(fm["title"], str) or not fm["title"].strip():
        errors.append(_err("title", "non-empty-string", "title must be a non-empty string"))
    if not isinstance(fm["source"], str) or not fm["source"].strip():
        errors.append(_err("source", "non-empty-string", "source must be a non-empty string"))
    if fm["type"] not in TYPE_VOCAB:
        errors.append(
            _err("type", "vocabulary", f"type '{fm['type']}' not in {sorted(TYPE_VOCAB)}")
        )
    if fm["status"] not in STATUS_VOCAB:
        errors.append(
            _err("status", "vocabulary", f"status '{fm['status']}' not in {sorted(STATUS_VOCAB)}")
        )

    msg = _check_created(fm["created"])
    if msg:
        errors.append(_err("created", "iso-date", msg))
    for list_field in ("subject", "relation"):
        msg = _check_str_list(fm[list_field], list_field)
        if msg:
            errors.append(_err(list_field, "string-list", msg))

    if body is None or not body.strip():
        errors.append(_err("body", "non-empty", "note body must not be empty"))

    return ValidationResult(ok=not errors, errors=errors, frontmatter=fm)


def validate(path: str, content: str) -> ValidationResult:
    """Combined check used by the writer. Errors from both phases merged."""
    pr = validate_path(path)
    cr = validate_content(content)
    return ValidationResult(
        ok=pr.ok and cr.ok,
        errors=pr.errors + cr.errors,
        frontmatter=cr.frontmatter,
    )
