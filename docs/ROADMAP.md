# Roadmap

- **Document type:** Design direction / forward intent
- **Status:** Directional — items here are logged intent, not scheduled commitments

This document records where the design is *heading*, so the direction is not lost, and so today's schema and architecture can be verified compatible with tomorrow's before either is built further. The distinction between logged intent and active work is deliberate: strategic direction is captured here without treating it as a competing priority against current milestones.

Items are classified as **Adopt** (a decided next step), **Watch** (evaluated, deferred pending a trigger), or **Held** (a design direction recorded but not committed).

---

## 1. Held — knowledge-graph extension

**The endpoint of the design direction, and the reason the metadata model is what it is.**

The vault's Dublin Core frontmatter is chosen so that lifting it into a formal **knowledge graph** is *additive*, not a rewrite:

- Each note is a candidate **node**; its `type` is a proto-node-type; its `relation` list is a set of proto-**edges**.
- Because the fields are RDF-compatible, each note projects cleanly into JSON-LD / YAML-LD with **no field renaming**.
- A single **ontology** governs valid node and edge types, reusing `schema.org` where it already covers a concept (`Organization`, `Person`, `SoftwareApplication`) and extending only where standard vocabulary does not reach.

**Trigger.** This becomes relevant when there is a concrete need to model a domain of typed entities supporting **multi-hop relational queries** across entity types — questions a document search fundamentally cannot answer (e.g. "which environments run an end-of-life OS tied to a contract expiring this quarter"). It is *not* triggered by the existence of tabular data alone; single-table needs stay tabular.

**Candidate shape (not committed).** An embedded graph store (e.g. Kuzu, or SQLite with a graph extension) rather than a standalone graph server, consistent with the local-first, minimal-moving-parts preference. Entities extracted from Markdown notes feed the graph via `relation` and `subject`, once an extraction mapping is designed.

**Open items before this could move to Adopt:** no concrete domain modeled yet; ingestion mapping undesigned; embedded graph engine unevaluated. See [VAULT-SCHEMA.md §4](VAULT-SCHEMA.md).

---

## 2. Watch — linked-data projection (YAML-LD / JSON-LD)

Adding `@context` / `@type` linked-data annotations would make each note a first-class linked-data resource. The Dublin Core field names already chosen are compatible with this projection, so it remains a clean additive step.

**Posture: watch, don't adopt.** Revisit only if semantic-similarity retrieval begins returning ambiguous results, or if relational (rather than similarity) queries become a real requirement. Until then it adds ceremony without payoff. The value of recording it now is confirming the current schema does not foreclose it.

---

## 3. Watch — embedding-model evolution

The embedding model is deliberately swappable (one config value). Migration triggers to watch:

- Cost or rate-limit pressure (not applicable to a local runtime, but relevant if a hosted model were ever used).
- A materially better local embedding model becoming available.
- Hot-path latency concerns at larger scale.

Any model change requires a full re-embed (`vault_reindex force=true`) because vector spaces are not comparable across models, and re-checking any model-specific query/document prefix asymmetry. At current vault scale, embedder quality is not the binding constraint, so this stays on watch.

---

## 4. Watch — index layer at scale

The in-process cosine ranking + poll-based indexer suit a small-to-moderate vault (order 10²–10³ notes). The layered architecture (durable storage / rebuildable index / swappable access) means the index layer can be replaced with a dedicated vector store **without touching the storage or access layers** if scale ever demands it. Recorded so the scaling path is explicit; deferred because the current design is scale-appropriate.

---

## 5. Adopt — lifecycle hygiene

Low-effort, high-value, and aligned with the core thesis: consistently marking superseded documents (`status: superseded`, `relation` pointing to the replacement) as designs evolve. This is the discipline that keeps context rot from re-accumulating over time. It is a practice, not a feature — but it is the practice the whole schema exists to enable.

---

## 6. Watch — interoperability standards

Emerging semantic-interoperability efforts (shared vocabularies and interchange formats for structured knowledge) are worth tracking, since the project's commitment to established vocabularies (Dublin Core, `schema.org`) positions it to adopt such standards additively rather than through migration. No action now; watch the space.
