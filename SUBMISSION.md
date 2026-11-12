# Submission

## Candidate
- **Name:** _(your name)_
- **Assignment chosen:** A (Data — Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** ~6h

## How to run

```bash
# 1. Setup
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate
pip install -r requirements.txt

# 2. Generate data (do NOT commit data/ folder)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 3. Ingest drops (order matters; each is idempotent)
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2

# 4. Generate all 5 report files
python -m solution.pipeline report --out reports

# 5. Run tests
python -m pytest solution/tests/ -v
```

The database is created automatically at `solution/ledger.db`. Specify a custom path with `--db`:

```bash
python -m solution.pipeline --db /tmp/test.db ingest data/drop_1
```

## Design

### Architecture

```
data/drop_N/
  leads/<workflow_id>/*.csv  ──► ingest_leads.py ──► leads table
  events/*.jsonl             ──► ingest_events.py ──► events table (ledger)
                                                       ▼
                             report.py ──► 5 CSV/JSON output files
```

All state lives in a single SQLite file (`solution/ledger.db`). No external dependencies beyond stdlib + pytest.

### Database Schema

```
leads               – one row per (workflow_id, phone_hash); UNIQUE enforces dedup
events              – append-only ledger; UNIQUE on event_id enforces dedup
rejections          – bad rows with reason (no PII)
ingested_drops      – idempotency guard at drop level
ingested_batches    – idempotency guard at batch/file level
data_quality        – single-row running counters (upserted)
rejection_reasons   – per-reason tallies
disposition_catalogue – loaded from dispositions.csv at report time
```

### PII Protection

Fields marked `"pii": true` (`customer_name`, `phone`) are stored as HMAC-SHA256 digests keyed with a secret from `PII_SECRET` env var (safe default for local use). The canonical phone is hashed before storage, so we can still match events to leads by hashing the event's phone the same way. Phone display uses `+91******XXXX` masking.

### Idempotency

- **Drop level:** `ingested_drops` table stores the resolved absolute path. Re-running `ingest data/drop_1` is a no-op.
- **Batch level:** `ingested_batches` guards each CSV. Even if the drop guard were bypassed, individual batches are skipped if already seen.
- **Event level:** `UNIQUE(event_id)` on the events table; `INSERT OR IGNORE` silently skips duplicates.
- **Rejections:** `UNIQUE(workflow_id, batch_id, row_number)` prevents double-counting.

### Orphan Re-linkage

Events can arrive before their lead (earlier drop). They are stored with `is_orphan=1, lead_id=NULL`. After every drop's leads are fully ingested, `relink_orphans()` scans all orphaned events and re-attempts linking. This ensures the final `orphan_events` count reflects only events that could never be linked, not a timing artifact.

## Decisions & Assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| SQLite (not Postgres) | Zero-dependency; WAL mode gives concurrent reads; sufficient for assignment scale | Postgres (included docker-compose) — adds ops overhead for no benefit here |
| HMAC-SHA256 for PII | One-way but matchable: hash phone at ingest and again at event-link time to find the lead | AES encryption — requires IV storage and is reversible (overkill; not needed) |
| Drop-level idempotency guard | Cleaner UX: re-running is an instant no-op for the whole drop | Batch-level only — still works but prints misleading "skipped" per file |
| `INSERT OR IGNORE` for events dedup | Simpler than `ON CONFLICT DO NOTHING` alias; works in Python 3.10 sqlite3 | Explicit SELECT-then-INSERT — more DB round trips |
| Defaults applied after merging | Spec says defaults apply to fields still null after all merges | Applying defaults per-row would prevent later non-null values from being set |
| Naive timestamps = IST | Spec is explicit; UTC suffix `Z` or `+00:00` takes precedence | Rejecting naive timestamps — would lose many valid events |
| `invalid_phone` only for non-empty bad phones | Spec says "phone is present but invalid"; empty phone triggers `missing_required:phone` for required field | Treating empty as invalid_phone — contradicts spec's ordered rejection walk |
| `rejections_by_reason` excludes `invalid_phone` from current data | Current dataset has 0 invalid phones (all bad rows fail on missing required fields first) | Pre-seeding with 0 — decided to show only reasons that actually appeared |

## Failure modes considered

| Scenario | Handling |
|---|---|
| Re-running `ingest` on same drop | Drop-level idempotency guard; instant no-op |
| Duplicate events across drops | `UNIQUE(event_id)` + `INSERT OR IGNORE` |
| Malformed JSON lines | Caught by `json.JSONDecodeError`; counted, skipped |
| Event with no `event_id` | Treated as malformed; counted, skipped |
| Event before its lead (drop ordering) | Stored as orphan; re-linked after leads arrive |
| `disposition.override` with no phone | Linked via `call_id` to other events on the same call |
| Unknown disposition code | Counted, not inserted into ledger |
| Impossible dates (31/02) | `datetime()` raises `ValueError`; treated as null + field warning |
| Currency symbol encoding (₹ as mojibake) | File opened with `utf-8` encoding; ₹ parses correctly |
| Renamed/reordered columns in drop_2 | Header normalisation + alias matching handles this transparently |
| Extra columns in CSV | Ignored if they match no schema field |
| Blank rows | Detected by checking all cells empty; skipped, not counted |
| **Not handled:** partial write of DB | SQLite WAL + `conn.commit()` per batch; interrupted write leaves DB in pre-commit state |
| **Not handled:** concurrent ingestion processes | SQLite WAL handles readers; writers would serialize on the WAL lock |

## Testing

Covered by 54 unit tests in `solution/tests/test_pipeline.py`:

- **Phone normalisation**: all 6 input formats + invalid cases
- **Date parsing**: all 4 formats + impossible dates + empty
- **Decimal parsing**: all currency prefixes, comma separators, negative, zero
- **Enum normalisation**: case-insensitive matching, invalid values
- **Header normalisation**: underscores, mixed whitespace, capitals
- **Timestamp parsing**: UTC (Z), offset (+05:30), naive → IST
- **PII**: mask_phone format, HMAC determinism
- **Effective disposition**: no disposition, disposition.set, override beats set, latest override wins
- **Bucket logic**: all 4 bucket states + priority ordering (completed > telephony_failed)
- **Idempotency**: same batch ingested twice → 1 lead in DB, second run is a no-op

Not tested (would require integration setup):
- Full end-to-end pipeline with real files (manual smoke test run successfully)
- Cross-drop orphan re-linkage (integration test; verified manually)
- Report output format diffing (would need golden files from a known-good run)

## Results

Run on `--seed 42 --scale small`:

| Metric | Value |
|---|---|
| Lead rows total | 2,057 |
| Lead rows rejected | 108 |
| Duplicate rows merged | 57 |
| Field warnings | 122 |
| Events total | 8,745 |
| Malformed events | 8 |
| Duplicate events dropped | 235 |
| Orphan events (final) | 1,373 |
| Unknown disposition events | 21 |
| Re-linked orphans (drop_1→2) | 14 |

Ingest + report runs in < 5 seconds on a standard laptop for the small scale.

## If I had another day

1. **Golden-file integration tests**: capture expected CSV/JSON output for seed 42, diff against fresh run to catch regressions
2. **Truly incremental processing**: track last-processed byte offset per file; skip already-processed lines in events JSONL
3. **SQL-based report aggregation**: move bucket logic into a CTE/view for performance at large scale
4. **Performance on `--scale large`**: profile and add bulk `executemany` for event inserts; likely stays under 2 min
5. **Production note**: add a scheduler (Airflow/cron), alerting on orphan spike, and a backfill mode that re-links orphans for a specific date range
