# Solution – Assignment A: Interaction Ledger & Reporting Pipeline

## Setup

```bash
cd sde-assignment-junior-2026
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

## Generate test data (if not already present)

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

## Run the pipeline

```bash
# Ingest drop 1
python -m solution.pipeline ingest data/drop_1

# Ingest drop 2
python -m solution.pipeline ingest data/drop_2

# Generate all 5 report files → reports/
python -m solution.pipeline report --out reports
```

Re-running any ingest command is safe (idempotent). Custom DB path:

```bash
python -m solution.pipeline --db /path/to/custom.db ingest data/drop_1
```

## Run tests

```bash
pytest solution/tests/ -v
```

## Design

See `../SUBMISSION.md` for full design notes.

**Database:** SQLite (`solution/ledger.db`). Tables:
- `leads` – one row per `(workflow_id, phone_hash)`. PII stored as HMAC-SHA256.
- `events` – append-only ledger; `is_orphan=1` for unlinked events.
- `rejections` – bad rows with reason (no PII).
- `data_quality` – single-row running counters.
- `ingested_drops` / `ingested_batches` – idempotency guards.
