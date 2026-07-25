# Vault Schema — Note Structure & Frontmatter Contract

- **Document type:** Canonical schema specification
- **Status:** Finalized (Sections 1–3, 5) / Provisional (Section 4)
- **Related:** [KNOWLEDGE-MODEL.md](KNOWLEDGE-MODEL.md) (the rationale), [ARCHITECTURE.md](ARCHITECTURE.md) (indexing/retrieval)

---

## 1. Purpose and scope

This document defines what a well-formed vault note looks like, and why. It answers a narrower question than the architecture reference: not "how does retrieval work," but "what structural contract must every note satisfy."

The schema exists to solve a specific, repeatedly observed failure mode: prior LLM-maintained knowledge bases produced inconsistent structure across notes because no explicit, small, typed field set was enforced. This was never fixed by a more capable model — **inconsistent schema enforcement is an architecture gap, not a model-capability gap.**

---

## 2. Architectural principles

The design layers three independent decisions, each justified separately so the whole does not depend on any single one holding forever.

### 2.1 File format and storage discipline

- Notes are plain Markdown files in a git repository.
- An agent or human reads and writes these files directly. The vault functions as raw knowledge with no running service and no mandatory retrieval pipeline.
- Rationale: plain text has minimal failure modes, needs no running service, and is trivially portable across any future harness. This is intentionally the least-clever option.
- The embedding/index layer (local Ollama + SQLite vector store) is a **derived, rebuildable cache** on top of this layer. If the index is lost, it is fully reconstructable from the Markdown alone.

### 2.2 Field vocabulary (Dublin Core subset)

- Frontmatter field names are drawn from the Dublin Core Metadata Element Set wherever a Dublin Core term applies, rather than inventing bespoke names.
- Rationale: Dublin Core is an established, RDF-compatible metadata vocabulary already used in Linked Data contexts. Using its names means any note can be projected into JSON-LD / YAML-LD later **without a field-renaming migration**, should a formal knowledge-graph layer be added (Section 4).
- Fields with no clean Dublin Core equivalent (notably `status`) are explicitly marked as vault-specific extensions, so the standard and the local additions are never confused.

### 2.3 Serialization convention (typed YAML frontmatter)

- Fields are a YAML frontmatter block at the top of each file, delimited by `---` lines — the convention popularized by Obsidian and broadly supported by Markdown tooling.
- This is a syntax choice only. Any tool that reads YAML-fenced Markdown frontmatter is compatible; there is no dependency on any particular editor.

---

## 3. The note schema

### 3.1 Core field reference

| Field | Dublin Core Term | Type | Required | Description |
|---|---|---|---|---|
| `title` | `dc:title` | string | Yes | Human-readable name of the note. |
| `type` | `dc:type` | string (controlled vocabulary) | Yes | Category of note. See §3.2. |
| `created` | `dc:date` | date (`YYYY-MM-DD`) | Yes | Date the note was first created. |
| `subject` | `dc:subject` | list of strings | Yes* | Topic/tag list. |
| `relation` | `dc:relation` | list of strings | Yes* | References to other notes, files, or identifiers this note relates to. |
| `source` | `dc:source` | string | Yes* | Originating conversation, URL, or document. |
| `status` | — (vault-specific extension) | string (controlled vocabulary) | Yes | Lifecycle state. See §3.3. |

*The write validator requires all seven fields to be **present**. `subject` and `relation` may be empty lists and `source` may be a placeholder string, but the keys must exist — presence is enforced so that provenance is never silently omitted.

### 3.2 Controlled vocabulary — `type`

An unconstrained free-text `type` defeats the purpose of having one. The set is small and evolves deliberately:

- `decision` — a finalized architectural or operational decision, with rationale.
- `runbook` — operational/procedural instructions (setup, recovery, how-to).
- `note` — general working note or observation.
- `glossary` — term definition(s).
- `reference` — externally-sourced material summarized or excerpted for reuse.
- `specification` — a formal spec or schema definition.

New `type` values are added deliberately, never coined ad hoc per note, to keep the vocabulary small and queryable. A value outside this set is a **hard validation error** on write.

### 3.3 Controlled vocabulary — `status`

- `draft` — not yet reviewed or finalized.
- `active` — current and in effect.
- `superseded` — replaced by a newer note; `relation` should point to the replacement.

Search defaults to `active` (and unfiltered current material) only. `superseded` notes are retained and remain retrievable on explicit request, but are excluded from ordinary retrieval. This field is the structural mechanism by which current truth is preferred over retired material.

### 3.4 Example

```markdown
---
title: "MCP Cold-Start Race Condition"
type: runbook
created: 2026-06-30
subject: [mcp, cold-start, uv]
relation: [deployment-notes-v1.0.md]
source: "operator notes, June 2026"
status: active
---

Some MCP clients enforce an internal `initialize` handshake timeout. On a
host's first run, compiling a native dependency from source can exceed that
window, causing the connection to fail before the server is ready...
```

### 3.5 Enforcement

Schema compliance is **enforced automatically at write time** by `ks/validator.py`, invoked by the `vault_write` tool before any content reaches disk. Enforced rules:

- A well-formed `---`-delimited YAML frontmatter block must be present and parse to a mapping.
- All seven required fields must be present.
- `type` and `status` must be members of their controlled vocabularies.
- `created` must be a valid ISO date; `subject` and `relation` must be lists of non-empty strings; `title` and `source` must be non-empty strings; the body must be non-empty.
- Path rules: writes confined to the designated writable subtree; lowercase kebab-case filenames and directories; optional dotted-semver suffix; no absolute paths, `..` traversal, or symlink escape.

Every rejection returns a structured `{field, rule, message}` error so an agent caller can correct and retry programmatically. Compliance does not rely on the model "remembering" the schema — it is a machine-checked invariant.

---

## 4. Provisional: knowledge-graph extension

**Status: Exploratory / Held.** Nothing here is an implementation commitment. It is recorded so the design direction is not lost, and so the Section 3 schema is verified compatible with it before either is built further.

### 4.1 Trigger condition

This extension becomes relevant when there is a need to model a domain of typed entities (e.g. organizations, servers, applications, vendors, contracts, people, network segments) supporting **multi-hop queries spanning multiple entity types** — the kind a spreadsheet or document search cannot answer directly (e.g. "which environments run an end-of-life OS tied to a contract expiring this quarter"). It is not triggered by the mere existence of tabular data; single-table data that only needs single-table queries should remain tabular.

### 4.2 Conceptual model

- **Nodes:** typed entities.
- **Edges:** typed relationships (`runs`, `licensedBy`, `dependsOn`, `coveredBy`, `adminOf`, …).
- **Ontology:** a single reusable schema of allowed node/edge types, reusing `schema.org` vocabulary where it already covers a concept (`Organization`, `Person`, `SoftwareApplication`) and extending only where standard vocabulary does not reach.
- **Isolation:** one ontology definition; separate graph instances per bounded domain where data isolation is required.

### 4.3 Candidate implementation shape (not committed)

- An embedded graph store (e.g. Kuzu, or SQLite with a graph extension) rather than a standalone graph server, consistent with the local-first, minimal-moving-parts preference.
- Structured data mapped into nodes/edges via a defined ingestion mapping.
- Entities from Markdown notes can feed the graph via `relation` and `subject`, once a concrete extraction process is designed.

### 4.4 Open items before finalizing

- No concrete domain modeled yet — premature to fix entity definitions against a hypothetical.
- Ingestion mapping (structured data → graph) is undesigned.
- Embedded graph engine unevaluated (Kuzu named as a candidate, not a decision).

---

## 5. Assumptions, constraints, and risks

- **Constraint:** The schema is additive. Existing notes are brought into compliance opportunistically (on next edit), not via bulk rewrite, unless a bulk rewrite is separately requested.
- **Risk (mitigated):** Earlier iterations relied on agent instructions for compliance, which could drift on a model swap. This is now mitigated by write-time validation (§3.5) — drift is caught mechanically, not by convention.
- **Risk:** The Section 4 graph direction is speculative and unscheduled. Treat as directional intent, not a deliverable.

---

## 6. Glossary

- **Frontmatter:** structured metadata (YAML) at the top of a Markdown file, delimited by `---`.
- **Dublin Core:** an established, RDF-compatible metadata vocabulary (title, subject, date, type, source, relation, …) for describing resources in a domain-agnostic, machine-interoperable way.
- **Controlled vocabulary:** a small fixed set of permitted values for a field, making the corpus classified and queryable.
- **Triple:** a single graph fact — subject → predicate → object.
- **Ontology:** the rule set defining valid entity and relationship types in a knowledge graph.
- **Node / Edge:** a graph entity / a labeled relationship between two entities.
- **Harness:** the runtime scaffold around a model providing tools, context, and I/O. Models and harnesses are independently swappable; the vault is designed to outlive both.
- **Storage / Index / Access layering:** Markdown vault = durable storage; vector store = rebuildable index; MCP = access layer.
