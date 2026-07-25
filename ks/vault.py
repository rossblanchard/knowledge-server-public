"""Vault file access: scanning, frontmatter parsing, markdown-aware chunking.

Read-side parsing is deliberately LENIENT (missing/malformed frontmatter
indexes with null fields). Strict validation belongs to the write path
(vault_write, M3) and the pre-commit hook — spec §8 layering.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class VaultFile:
    relpath: str
    content_hash: str
    mtime: float
    frontmatter: dict
    body: str


@dataclass
class Chunk:
    heading_path: str
    text: str


def scan_vault(vault_dir: Path = config.VAULT_DIR) -> list[str]:
    """Return relative POSIX paths of all indexable markdown files."""
    found: list[str] = []
    for root in config.INDEX_ROOTS:
        base = vault_dir / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            found.append(p.relative_to(vault_dir).as_posix())
    for name in config.EXTRA_FILES:
        if (vault_dir / name).is_file():
            found.append(name)
    return found


def load_file(relpath: str, vault_dir: Path = config.VAULT_DIR) -> VaultFile:
    path = vault_dir / relpath
    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)
    return VaultFile(
        relpath=relpath,
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        mtime=path.stat().st_mtime,
        frontmatter=fm,
        body=body,
    )


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a note into (frontmatter dict, body). Lenient on failure."""
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, raw[match.end():]
    if not isinstance(data, dict):
        data = {}
    return data, raw[match.end():]


def as_list(value) -> list[str]:
    """Normalize a frontmatter field that may be scalar, list, or absent."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def chunk_body(body: str, fallback_title: str = "") -> list[Chunk]:
    """Markdown-aware chunking.

    Split on ATX headings (outside code fences), tracking the heading
    stack so each chunk carries its heading path (e.g. "Build Plan > M1")
    for citation context. Sections longer than CHUNK_TARGET_CHARS are
    split at paragraph boundaries with CHUNK_OVERLAP_CHARS of overlap.
    """
    chunks: list[Chunk] = []
    for heading_path, text in _split_sections(body, fallback_title):
        text = text.strip()
        if not text or _is_heading_only(text):
            continue
        if len(text) <= config.CHUNK_TARGET_CHARS:
            chunks.append(Chunk(heading_path, text))
        else:
            chunks.extend(Chunk(heading_path, t) for t in _split_long(text))
    return chunks


def _is_heading_only(text: str) -> bool:
    """True if a section contains nothing beyond its own heading line.

    Such sections (a heading immediately followed by subheadings) carry
    no retrievable content of their own — the heading text already
    appears in every child chunk's heading_path — and they score
    artificially well on keyword-adjacent queries.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) == 1 and bool(HEADING_RE.match(lines[0]))


def _split_sections(body: str, fallback_title: str) -> list[tuple[str, str]]:
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    current: list[str] = []
    in_fence = False

    def flush():
        if current:
            heading_path = " > ".join(t for _, t in stack) or fallback_title
            sections.append((heading_path, "\n".join(current)))
            current.clear()

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            current.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            current.append(line)
        else:
            current.append(line)
    flush()
    return sections


def _split_long(text: str) -> list[str]:
    target = config.CHUNK_TARGET_CHARS
    overlap = config.CHUNK_OVERLAP_CHARS

    # Break into paragraph-sized units; hard-slice any single unit
    # (giant table, code block) that alone exceeds the target.
    units: list[str] = []
    for para in re.split(r"\n\n+", text):
        if not para.strip():
            continue
        if len(para) <= target:
            units.append(para)
        else:
            start = 0
            while start < len(para):
                units.append(para[start : start + target])
                if start + target >= len(para):
                    break
                start += target - overlap

    # Greedily pack units up to the target, carrying tail overlap forward.
    parts: list[str] = []
    buf = ""
    for unit in units:
        candidate = f"{buf}\n\n{unit}" if buf else unit
        if len(candidate) > target and buf:
            parts.append(buf)
            carried = buf[-overlap:]
            buf = f"{carried}\n\n{unit}"
            if len(buf) > target:
                buf = unit
        else:
            buf = candidate
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]
