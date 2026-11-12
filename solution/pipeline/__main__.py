"""
CLI entry point.

Usage:
  python -m solution.pipeline ingest <drop_path>
  python -m solution.pipeline report --out <output_dir> [--db <db_path>]

The database defaults to solution/ledger.db.
Schemas are auto-discovered from assignment-a-data/schemas/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve project root (two levels up from this file)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMAS_DIR = PROJECT_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB = PROJECT_ROOT / "solution" / "ledger.db"


def _load_context(db_path: Path):
    """Return (conn, schemas, dispositions)."""
    from .db import init_db
    from .schema_loader import load_all_schemas, load_dispositions

    conn = init_db(db_path)
    schemas = load_all_schemas(SCHEMAS_DIR)
    disps = load_dispositions(SCHEMAS_DIR / "dispositions.csv")
    return conn, schemas, disps


def cmd_ingest(args):
    from .db import init_db
    from .schema_loader import load_all_schemas, load_dispositions
    from .ingest_leads import ingest_leads
    from .ingest_events import ingest_events, relink_orphans

    drop_path = Path(args.drop_path).resolve()
    db_path = Path(args.db).resolve()

    if not drop_path.exists():
        print(f"ERROR: drop path not found: {drop_path}", file=sys.stderr)
        sys.exit(1)

    conn, schemas, disps = _load_context(db_path)

    # Idempotency: skip entire drop if already ingested
    already = conn.execute(
        "SELECT 1 FROM ingested_drops WHERE drop_path=?", (str(drop_path),)
    ).fetchone()
    if already:
        print(f"Drop already ingested (idempotent skip): {drop_path}")
        return

    # Determine drop_sequence for event ordering
    seq = conn.execute(
        "SELECT COUNT(*) as c FROM ingested_drops"
    ).fetchone()["c"] + 1

    print(f"\n{'='*60}")
    print(f"Ingesting drop: {drop_path}  (sequence={seq})")
    print(f"{'='*60}")

    # ── Ingest leads ──────────────────────────────────────────────────────────
    leads_root = drop_path / "leads"
    if leads_root.exists():
        for workflow_dir in sorted(leads_root.iterdir()):
            if not workflow_dir.is_dir():
                continue
            workflow_id = workflow_dir.name
            if workflow_id not in schemas:
                print(f"  [WARN] Unknown workflow '{workflow_id}', skipping.")
                continue
            schema = schemas[workflow_id]

            for csv_file in sorted(workflow_dir.glob("*.csv")):
                batch_id = csv_file.stem  # e.g. "batch_001"
                print(f"  Leads [{workflow_id}] {csv_file.name} ...", end=" ")
                stats = ingest_leads(conn, csv_file, workflow_id, batch_id, schema)
                if stats.get("skipped"):
                    print("skipped (already ingested)")
                else:
                    print(
                        f"total={stats['total']}  new={stats['new_leads']}  "
                        f"merged={stats['merged']}  rejected={stats['rejected']}  "
                        f"warnings={stats['warnings']}"
                    )

    # ── Ingest events ─────────────────────────────────────────────────────────
    events_root = drop_path / "events"
    if events_root.exists():
        for jsonl_file in sorted(events_root.glob("*.jsonl")):
            print(f"  Events {jsonl_file.name} ...", end=" ")
            stats = ingest_events(conn, jsonl_file, disps, drop_sequence=seq)
            print(
                f"total={stats['total']}  inserted={stats['inserted']}  "
                f"malformed={stats['malformed']}  dup={stats['duplicate']}  "
                f"orphan={stats['orphan']}  unknown_disp={stats['unknown_disposition']}"
            )

    # ── Re-link orphans (events whose lead arrived in a later drop) ───────────
    linked = relink_orphans(conn, disps)
    if linked:
        print(f"  Re-linked {linked} previously-orphaned events.")

    # Mark drop as done
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR IGNORE INTO ingested_drops (drop_path, ingested_at) VALUES (?,?)",
        (str(drop_path), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    print(f"Drop ingested successfully.\n")


def cmd_report(args):
    from .report import generate_reports

    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()

    conn, _, _ = _load_context(db_path)
    disps_path = SCHEMAS_DIR / "dispositions.csv"

    print(f"Generating reports → {out_dir}")
    generate_reports(conn, out_dir, disps_path)
    print("Done. Files written:")
    for f in sorted(out_dir.iterdir()):
        print(f"  {f.name}")


def main():
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Interaction Ledger & Reporting Pipeline",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Path to the SQLite database (default: solution/ledger.db)",
    )

    sub = parser.add_subparsers(dest="command")

    p_ingest = sub.add_parser("ingest", help="Ingest a data drop")
    p_ingest.add_argument("drop_path", help="Path to drop directory (e.g. data/drop_1)")

    p_report = sub.add_parser("report", help="Generate report files")
    p_report.add_argument(
        "--out", default="reports", help="Output directory (default: reports/)"
    )

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "report":
        cmd_report(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

