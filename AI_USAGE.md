# AI Usage

## Tools used
- **Antigravity (Claude Sonnet)** — primary assistant throughout

## How AI was used

| Task | AI contribution |
|---|---|
| Initial project exploration | AI read and summarised all README/schema files |
| Database schema design | AI proposed the schema; reviewed and approved the PII approach |
| `normalise.py` | AI generated all parsers; manually verified against spec rules (phone prefixes, date formats, decimal prefixes) |
| `ingest_leads.py` | AI generated; reviewed header-map logic and duplicate-merge behaviour against spec |
| `ingest_events.py` | AI generated; reviewed orphan re-linkage and processing order against spec |
| `report.py` | AI generated bucket logic and all 5 report functions; verified output format against contract |
| `__main__.py` CLI | AI generated; reviewed idempotency guard and argument structure |
| Test suite | AI generated 54 tests; all reviewed for correctness before running |
| `SUBMISSION.md` | AI drafted; content reflects actual decisions made during implementation |

## What I verified and can defend

- Every phone format tested manually and against spec
- Bucket priority order (completed > telephony_failed > dialed) cross-checked with spec
- Orphan re-linkage logic (why it must run after leads are ingested)
- PII approach: why HMAC-SHA256 allows matching without storing plaintext
- Idempotency: how three layers of guards (drop, batch, event_id) work together
- Why `INSERT OR IGNORE` is safe for deduplication here

## Code I can change live

I can:
- Add a new field type to the normalisation layer
- Change the bucket priority or add a new bucket
- Add a new report output file
- Switch from SQLite to Postgres (change `get_connection` only)
- Add a new rejection reason
