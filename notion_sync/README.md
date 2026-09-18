# Notion read-only incremental collection

This directory implements the local `STAGING` side of the Notion ingestion flow.
It does not call Notion directly and cannot write to Notion. A read-only provider
(currently the Codex Notion connector) exports a snapshot JSON, and
`incremental_ingest.py` validates, redacts, hashes, and stores changed documents.
The bundled initial seed is a deterministic synthetic test/bootstrap example,
not a canonical export and not operational Notion authority. Real workspace
resource identities and provider snapshots remain outside the public source
baseline; the preserved private canonical history retains their provenance.
Runtime adapters resolve the private reservation and cleaning data-source bindings only from
`PROPERTYAI_NOTION_RESERVATION_SOURCE_ID` and `PROPERTYAI_NOTION_CLEANING_SOURCE_ID`;
missing or malformed bindings fail closed before an external request is made.

## Safety model

- No Notion create, update, archive, delete, or comment operations.
- Actual reservation, contract, finance, Person Private, and access-code rows are
  excluded from the initial scope.
- Email addresses, Korean phone numbers, resident-registration-number patterns,
  and Airbnb-style confirmation codes are redacted before persistence.
- A partial discovery run never treats an absent document as deleted.
- A complete inventory must omit a document three consecutive times before it is
  labelled `tombstone_candidate`; local content is still retained.
- STAGING files are immutable by content hash and are not used by production RAG.

## Usage

```bash
python3 notion_sync/incremental_ingest.py notion_sync/fixtures/initial_seed.json
python3 -m unittest discover -s notion_sync/tests -v
```

Generated paths:

```text
notion_sync/runtime/
├── staging/<source_id>/<sha256>.json
├── validated/<source_id>/<sha256>.json
├── active/manifest.json
├── active/history/<generation_id>.json
├── state/state.json
└── runs/<run_id>.json
```

Validate and promote the privacy-minimized policy index with:

```bash
python3 notion_sync/index_pipeline.py --validate-only
python3 notion_sync/index_pipeline.py
```

The current seed becomes `SAFE_POLICY_INDEX` and is explicitly marked
`production_rag_eligible: false` because its pages are safe excerpts rather than
canonical complete exports. The ACTIVE manifest is not wired into production RAG.
The current connector is interactive through Codex; unattended collection will
require a separately approved Notion integration credential stored in macOS
Keychain, never in this workspace.

After all required pages are canonical and validation has no warnings:

```bash
python3 notion_sync/active_corpus.py
python3 notion_sync/benchmark_active_rag.py
```

The benchmark uses a generation-specific embedding cache and writes a separate
`outputs/rag-canonical-active.json`; it does not replace the existing RAG corpus.
