# The Knowledge Model

**Document type:** Canonical architecture rationale
**Status:** Finalized
**Audience:** Systems architects and engineers evaluating the design

This document explains *why* this system is built the way it is. It is the intellectual center of the repository. The code exists to serve the model described here; if you read only one document, read this one.

---

## 1. Purpose

To state, precisely and defensibly, the thesis behind this project: that a durable machine-maintained knowledge base requires a **governed semantic layer** — a controlled vocabulary, a metadata ontology, and an enforced lifecycle — sitting *above* a vector index, and that this structure must be enforced at **write time**. The claim is that retrieval quality is primarily an architecture property, not a model-capability property.

---

## 2. The core distinction: recall versus comprehension

Two words are often used interchangeably when people describe giving an AI a "memory." They should not be.

- **Recall** is the ability to retrieve stored material that resembles a query. A vector database provides recall: text is embedded into a high-dimensional space, and a query returns the nearest neighbors by cosine similarity. This is genuinely useful and genuinely a form of operating on *meaning* — embeddings capture semantic similarity, so "how do I restart the service" can retrieve a document titled "service recovery procedure" even with no shared keywords.

- **Comprehension**, in the sense used here, is the ability to reason about the *status of knowledge itself*: to know which of two similar documents is authoritative, which has been retired, what category each belongs to, when each was written, and how they relate. Comprehension is what lets a system prefer the current truth over a well-phrased obsolete one.

### 2.1 An honest note on the word "understand"

This system does not make a language model "understand" in any strong or human sense, and this document will not pretend otherwise. The embedding model computes geometric proximity; it has no concept of authority or time. What the *system* does — and this is the defensible version of the claim — is engineer an explicit, machine-checkable structure around each document so that the **retrieval layer behaves as if it comprehends the corpus**: it can distinguish a decision from a runbook, an active note from a superseded one, and a primary source from a derivative, because those properties are *declared and enforced* rather than inferred. Understanding, here, is a property manufactured by structure, not an emergent capability of the model. That is a more modest claim, and it is the one this repository can actually support.

---

## 3. The failure mode this design targets

Bare retrieval-augmented generation degrades as it scales. The failure has a specific shape, sometimes called **context rot**:

- **Staleness.** A superseded runbook is embedded and, being well-written, ranks highly forever. The system has no notion that it was replaced.
- **Contradiction.** An early decision and its later reversal coexist in the index. A query surfaces whichever is more textually similar, not whichever is true.
- **Duplication.** Drafts, near-duplicates, and copies compound, diluting relevance and multiplying the surface for contradiction.
- **No provenance.** Nothing records where a document came from or what it depends on, so authority cannot be assessed and blast radius cannot be traced.

The critical observation: **a more capable model does not repair this.** Inconsistent structure across documents is a gap in the *system's* design, not a deficiency the model can reason its way past. Prior attempts at LLM-maintained knowledge bases produced inconsistent structure precisely because no explicit, small, typed field set was enforced — and the fix was never a bigger model. It was enforcement.

---

## 4. Semantics: what embeddings give you, and what they do not

**Semantics** means the encoding of meaning. This system operates on meaning at two distinct levels, and conflating them is the root of the naive-RAG mistake.

- **Distributional / geometric semantics (the vector layer).** Embeddings place text in a space where proximity approximates relatedness of meaning. This is powerful for fuzzy, natural-language retrieval. But it is *implicit* meaning: nothing about a point in the space tells you the document's type, currency, or origin.

- **Declared / symbolic semantics (the governed layer).** Meaning that is written down as typed, validated metadata: *this document is a decision; it is active; it derives from that source; it relates to those notes.* This is *explicit* meaning — inspectable, filterable, and enforceable.

A robust knowledge system needs both. The vector layer finds candidates by resemblance; the governed layer decides which candidates are admissible and authoritative. This repository's central design move is to make the second layer **first-class and mandatory**, not an optional afterthought.

---

## 5. The governed semantic layer

Every note carries a small block of typed frontmatter. Small is deliberate: an unconstrained field defeats the purpose of having one, and a large schema is not maintained consistently in practice. The full field contract lives in `VAULT-SCHEMA.md`; the conceptual roles are:

### 5.1 A shared, established vocabulary (metadata ontology)

Field names are drawn from **Dublin Core** — an established, RDF-compatible metadata vocabulary — wherever a Dublin Core term applies (`title`/`dc:title`, `type`/`dc:type`, `subject`/`dc:subject`, `created`/`dc:date`, `source`/`dc:source`, `relation`/`dc:relation`). Two reasons this matters:

1. **Interoperability.** Using a standard vocabulary means the vault speaks a language other tools already understand, rather than a private dialect that must be translated at every boundary.
2. **A clean upgrade path.** Because these are RDF-compatible terms, any note can later be projected into JSON-LD / YAML-LD and lifted into a formal knowledge graph **without renaming a single field.** The vocabulary chosen today is compatible with the graph that might be built tomorrow (§8).

Fields with no clean Dublin Core equivalent (notably `status`) are marked explicitly as **vault-specific extensions**, so the standard and the local additions are never confused. Honesty about the boundary of a standard is part of using it well.

### 5.2 Controlled vocabularies (typed classification)

Two fields draw from small, fixed sets rather than free text:

- **`type`** — the category of artifact (`decision`, `runbook`, `note`, `glossary`, `reference`, `specification`). This is the corpus's **taxonomy**: it lets a query say "search only decisions" and makes the knowledge base *queryable by kind*, not just by content. New types are added deliberately, never coined per-note, so the vocabulary stays small and stable.
- **`status`** — the lifecycle state (below).

A controlled vocabulary is what turns a pile of documents into a *classified* corpus. It is the difference between a drawer of papers and a filing system.

### 5.3 The lifecycle model (`status`) — the structural cure for context rot

`status` is the single most important field. It takes one of three values:

- **`draft`** — provisional; not yet authoritative.
- **`active`** — current, authoritative knowledge.
- **`superseded`** — replaced by a newer note; its `relation` field points to the replacement.

Search **defaults to `active` only.** Superseded material is not deleted — it is retained in an `archive/` region, still indexed, still retrievable *on explicit request* for audit or historical reasoning — but it is excluded from ordinary retrieval. This is the direct architectural answer to staleness and contradiction: the well-phrased ghost is still on file, but it no longer haunts the results. The system prefers current truth because currency is a **declared, enforced property**, not something the embedding space could ever represent.

### 5.4 Provenance and relation (traceability)

- **`source`** records where a note came from (a conversation, a URL, an upstream document).
- **`relation`** records what a note references or depends on — including, for a superseded note, the note that replaced it.

Together these make the corpus **auditable and navigable**: authority can be assessed by tracing origin, and the impact of a change can be traced through relations. They are also the seed data for a future graph (§8): a `relation` is a proto-edge.

---

## 6. Enforcement at write time: correctness by construction

A schema that is merely *documented* is a schema that is *inconsistently followed*. This system does not document the schema and hope; it **enforces it at the only moment that matters — the write** — so that no malformed or unclassifiable document can ever enter the corpus.

The write tools (`vault_write`, `vault_patch`, `vault_archive`) reject, before touching disk, any content that fails:

- **Structural rules** — a well-formed YAML frontmatter block must be present and parse to a mapping.
- **Completeness rules** — every required field must be present.
- **Vocabulary rules** — `type` and `status` must be members of their controlled sets; an out-of-vocabulary value is a hard error, not a silent acceptance.
- **Type rules** — `created` must be a valid ISO date; `subject` and `relation` must be lists of non-empty strings; the body must be non-empty.
- **Path rules** — writes are confined to a designated writable subtree; filenames and directories must follow the lowercase kebab-case convention, with an optional dotted-semver suffix for explicit versioning; absolute paths, `..` traversal, and symlink escapes are all refused.

Every rejection returns a **structured, machine-actionable error** (`{field, rule, message}`), so an agent caller can correct and retry programmatically rather than guessing.

Two further disciplines make the write path safe for autonomous agents:

- **Two-phase writes.** A caller first issues a `dry_run`: full validation report plus, when overwriting, a unified diff — with **no mutation of any kind.** A real write follows only after explicit human approval. The system is designed so that agents propose and humans dispose.
- **Versioned, attributed history.** Each successful write is a single git commit under a fixed author identity, with the originating client recorded in the commit body. Nothing is overwritten in the sense of being lost — the full history is preserved, and supersession is a metadata transition, not a deletion.

The consequence is worth stating plainly: **the only way to add knowledge is to add well-formed, classified, provenanced knowledge.** Structure is not a request made of the model; it is an invariant of the system.

---

## 7. Versioning and the archive model

Two mechanisms provide durability and safe evolution:

- **File-level versioning** via the dotted-semver filename suffix (`…-v1.2.md`) plus the `status` transition. A new version is written as `active`; its predecessor is moved to `archive/` and marked `superseded`, with `relation` pointing forward to the replacement. History is legible at the level of the vault, not just the git log.
- **Commit-level versioning** via per-write git commits. Every state of every note is recoverable; every change is attributed. The git history *is* the audit trail.

Superseded notes remaining indexed-but-excluded is what lets the system answer "what did we used to believe, and why did it change?" without polluting the answer to "what is true now?"

---

## 8. Why this is additive toward a knowledge graph

The endpoint of this design direction is a formal **knowledge graph**, and the important property is that reaching it requires *no rewrite* of what already exists.

- A **knowledge graph** represents facts as **triples** — *subject → predicate → object* (e.g. *service-A → depends-on → database-B*). Entities become **nodes**; relationships become typed **edges**; and the whole is governed by an **ontology**: the rule set defining which node types and edge types are valid.
- The vault is already most of the way there. Each note is a candidate node; its `type` is a proto-node-type; its `relation` list is a set of proto-edges; and its Dublin Core frontmatter projects cleanly into JSON-LD / YAML-LD with no field renaming.
- The forward path reuses **`schema.org`** vocabulary where it already covers a concept (e.g. `Organization`, `Person`, `SoftwareApplication`) and extends it only where standard vocabulary does not reach. This unlocks **multi-hop relational queries** that a document search fundamentally cannot answer — the kind that span multiple entity types and follow chains of relationships.

This is recorded as a **design direction, held — not an implementation commitment.** It is documented so the direction is not lost, and so today's schema is verified compatible with tomorrow's graph before either is built further. Building the semantic layer correctly now is what makes the graph an *extension* later rather than a *migration*.

---

## 9. Design principles, summarized

1. **Governance above recall.** A vector index is necessary but not sufficient; the governed metadata layer is what makes retrieval trustworthy.
2. **Enforce at write, not read.** Correctness that is checked only at retrieval is correctness that was never guaranteed. The write is the chokepoint.
3. **Standard vocabularies over bespoke ones.** Dublin Core and `schema.org` buy interoperability and a migration-free upgrade path.
4. **Small, controlled vocabularies.** A field that permits anything classifies nothing.
5. **Status is first-class.** Currency is a declared property; it is the structural cure for context rot.
6. **Preserve, don't delete.** Supersession is a lifecycle transition; history is an asset, not clutter.
7. **Additive evolution.** Today's structure is chosen so that graphs, linked data, and richer ontologies extend it rather than replace it.

---

## 10. Glossary

- **Semantics** — the encoding of meaning. Here, both *geometric* (embedding proximity) and *declared* (typed metadata).
- **Embedding** — a numeric vector representing text, positioned so that semantically related text is nearby. Enables similarity search.
- **Cosine similarity** — the ranking measure for retrieval: the angle between a query vector and a document vector.
- **Context rot** — the degradation of retrieval quality as a knowledge base accumulates stale, contradictory, or duplicate material that a bare index cannot deprioritize.
- **Controlled vocabulary** — a small, fixed set of permitted values for a field (here, `type` and `status`), making the corpus classified and queryable by kind and state.
- **Taxonomy** — a classification scheme; in this vault, the `type` vocabulary and the `library/<category>/<subject>/` directory structure.
- **Ontology** — the rule set defining valid entity types and relationship types. Applied loosely to the metadata schema today; applied formally to the knowledge graph in the roadmap.
- **Dublin Core** — an established, RDF-compatible metadata vocabulary (title, subject, date, type, source, relation, …) for describing resources in a domain-agnostic, machine-interoperable way.
- **Provenance** — the recorded origin and dependency chain of a piece of knowledge (`source`, `relation`), enabling audit and impact analysis.
- **Lifecycle / status** — the declared currency of a note (`draft`, `active`, `superseded`); the mechanism by which current truth is preferred over retired material.
- **Triple** — a single graph fact: *subject → predicate → object*.
- **Knowledge graph** — a network of typed nodes and typed edges governed by an ontology, supporting multi-hop relational queries.
- **MCP (Model Context Protocol)** — the open protocol by which this vault is exposed to agents, making the knowledge layer harness-agnostic.
