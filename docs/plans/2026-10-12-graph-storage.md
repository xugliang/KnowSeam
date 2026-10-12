# KnowSeam optional graph storage implementation

Approved scope: a general knowledge service, not a product A workflow. PostgreSQL
is authoritative; Neo4j is an optional, rebuildable evidence projection. Standard
deployments do not import the Neo4j driver, connect to it, or call an extraction LLM.
Existing MCP/API list/search/read contracts and SuiteHarness remain unchanged.

## Execution constraints

- Authoritative directory: `/srv/knowseam`; no new server checkout.
- Preserve existing uncommitted changes, credentials, databases and A/B services.
- Local publication mirror is used only as an authorized patch assembly area;
  changed source hashes must match the original server before applying the patch.
- Tests use separately isolated PostgreSQL and Neo4j runtimes, synthetic documents
  and deterministic inference. Real databases are required for the integration
  contract; no claim of real-model quality follows from these tests.
- Do not enable graph on the existing production deployment or push GitHub.

## Components and tasks

1. Extract KnowledgeRepository and RetrievalBackend ABC contracts. Preserve
   PostgreSQL transactions/BM25/ACL and remove application use of private methods.
2. Define strict graph configuration, per-collection ontology (entity/relation
   types and explicit aliases), structured chunk extraction, evidence validation,
   GraphStore ABC and Neo4j projection adapter. Lazy optional driver dependency.
3. Add transactional PostgreSQL outbox, leased claims, bounded retry, immutable
   extraction checkpoints, obsolete-event handling and rebuild command.
4. Graph worker: extract current authorized chunks, persist checkpoints, replace
   document projection idempotently with monotonic event sequence fencing.
5. Search: seed only from authorized retrieved chunks; expand at most two hops;
   check every edge against current PostgreSQL evidence before expanding again;
   fuse/rerank original chunks, not synthesized graph summaries. Final ACL check.
   Graph failures degrade explicitly to normal vector+BM25 retrieval, not a fake
   graph success. Base retrieval failures retain existing fail behavior.
6. Formal Compose standard and graph modes, deployment/operations docs, tests and
   independent final review. Record what is implemented versus activated.

## Graph representation

Entity identity is scoped by company, collection, type, canonical name and optional
qualifier. Explicit configured aliases are applied at extraction time; automatic
cross-collection merging is forbidden. Each entity mention and relationship has
an exact quote and chunk/version/document reference. A relationship stores whether
it is explicit or inferred and confidence; inferred relationships are excluded by
default. Validity intervals are optional, timezone-aware. The graph returns opaque
entity IDs and evidence references to KnowSeam, not unrestricted Cypher results.

No graph-wide/community summary is generated: such summaries would mix different
employees' readable documents. PostgreSQL rehydrates graph candidates and checks
current company, collection grant, employee status, source enablement, source ACL
freshness, active document version and chunk identity before use.

## Acceptance

- Off mode: existing config/tests/MCP contracts work without neo4j installed.
- Atomic publication creates a graph event in the same PostgreSQL transaction.
- Recover after worker failure, retry no more than three failed attempts, no
  stale worker overwrite of a newer graph projection.
- Current documents reindex by version and extraction/ontology fingerprint;
  revocations/deletions are unreadable immediately even before projection cleanup.
- Explicit graph rebuild does not re-embed or re-download source documents.
- Unauthorized intermediate edge cannot connect an otherwise readable next hop.
- Actual PostgreSQL + Neo4j tests cover isolation, idempotency, revocation,
  version changes, deletion, extraction failures and two-hop search.

## Decisions

The first implementation supports one configured company per deployment, matching
the current service contract; all graph keys retain company scope. Multi-company
SaaS administration is not silently added. Graph extraction requires an explicitly
configured OpenAI-compatible chat endpoint separate from embedding/reranking.
Real online-model quality/cost is an operational evaluation, not established by
deterministic tests. Existing production config stays graph-disabled.

## Acceptance review corrections

- Recheck source authorization/version before each new chunk inference.
- Retain seed/intermediate evidence through reranking; authorize all support paths
  in one final read, then apply result limit. Independent base-channel hits remain.
- Restored unchanged documents enqueue after their ready transition, atomically.
- Startup/configuration durably enqueue missing semantic fingerprints once.
  Query-only options do not invalidate extraction; old workers cannot claim jobs
  belonging to a different fingerprint.
- Actual Neo4j covers complete service search and concurrent version replacement.
