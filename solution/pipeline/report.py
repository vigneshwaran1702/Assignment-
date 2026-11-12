"""
Report generation — produces all 5 output files from the database.

Bucket logic (first match):
  1. yet_to_reach_out      – no calls at all
  2. completed             – any call has a terminal effective disposition
  3. in_progress_telephony_failed – every call has a telephony_failed event
  4. in_progress_dialed    – everything else
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalise import ist_date_str, ist_iso, to_ist


# ── Helpers ────────────────────────────────────────────────────────────────────

def _effective_disposition(
    conn: sqlite3.Connection,
    call_id: str,
) -> tuple[str | None, bool, bool]:
    """
    Returns (disposition_code, is_terminal, is_success) for a call.
    Priority: latest disposition.override > latest disposition.set > None.
    """
    # Latest override
    override = conn.execute(
        """SELECT e.payload, e.occurred_at
           FROM events e
           WHERE e.call_id=? AND e.event_type='disposition.override'
             AND e.is_orphan=0
           ORDER BY e.occurred_at DESC LIMIT 1""",
        (call_id,),
    ).fetchone()

    if override:
        payload = json.loads(override["payload"])
        code = payload.get("disposition_code")
    else:
        # Latest disposition.set
        dset = conn.execute(
            """SELECT e.payload FROM events e
               WHERE e.call_id=? AND e.event_type='disposition.set'
                 AND e.is_orphan=0
               ORDER BY e.occurred_at DESC LIMIT 1""",
            (call_id,),
        ).fetchone()
        if dset:
            payload = json.loads(dset["payload"])
            code = payload.get("disposition_code")
        else:
            return None, False, False

    # Look up terminal/success from disposition catalogue
    if code:
        row = conn.execute(
            """SELECT is_terminal, is_success
               FROM disposition_catalogue
               WHERE workflow_id=(
                   SELECT workflow_id FROM events
                   WHERE call_id=? AND workflow_id IS NOT NULL LIMIT 1
               ) AND code=?""",
            (call_id, code),
        ).fetchone()
        if row:
            return code, bool(row["is_terminal"]), bool(row["is_success"])
        return code, False, False

    return None, False, False


def _get_calls_for_lead(conn: sqlite3.Connection, lead_id: int) -> list[dict]:
    """
    Return one dict per call that has a call.initiated event linked to this lead.
    Each dict: {call_id, initiated_at_utc, connected, telephony_failed}
    """
    initiated = conn.execute(
        """SELECT call_id, occurred_at
           FROM events
           WHERE lead_id=? AND event_type='call.initiated' AND is_orphan=0
           ORDER BY occurred_at""",
        (lead_id,),
    ).fetchall()

    calls = []
    for row in initiated:
        call_id = row["call_id"]
        initiated_at = row["occurred_at"]

        connected = conn.execute(
            "SELECT 1 FROM events WHERE call_id=? AND event_type='call.connected' AND is_orphan=0",
            (call_id,),
        ).fetchone() is not None

        tel_failed = conn.execute(
            "SELECT 1 FROM events WHERE call_id=? AND event_type='call.telephony_failed' AND is_orphan=0",
            (call_id,),
        ).fetchone() is not None

        calls.append({
            "call_id": call_id,
            "initiated_at_utc": initiated_at,
            "connected": connected,
            "telephony_failed": tel_failed,
        })
    return calls


def _bucket(calls: list[dict], conn: sqlite3.Connection) -> str:
    if not calls:
        return "yet_to_reach_out"

    for c in calls:
        code, is_terminal, _ = _effective_disposition(conn, c["call_id"])
        if is_terminal:
            return "completed"

    if all(c["telephony_failed"] for c in calls):
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


# ── Disposition catalogue table (created at report time) ──────────────────────

def _ensure_disposition_catalogue(conn: sqlite3.Connection, disps_path: Path):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS disposition_catalogue (
               workflow_id TEXT, code TEXT, is_terminal INTEGER, is_success INTEGER,
               PRIMARY KEY (workflow_id, code))"""
    )
    import csv as _csv
    with open(disps_path, newline="", encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            conn.execute(
                """INSERT OR IGNORE INTO disposition_catalogue
                   (workflow_id, code, is_terminal, is_success) VALUES (?,?,?,?)""",
                (
                    row["workflow_id"],
                    row["code"],
                    1 if row["is_terminal"].lower() == "true" else 0,
                    1 if row["is_success"].lower() == "true" else 0,
                ),
            )
    conn.commit()


# ── 1. batch_summary.csv ──────────────────────────────────────────────────────

def report_batch_summary(conn: sqlite3.Connection, out_dir: Path):
    leads = conn.execute(
        "SELECT id, workflow_id, batch_id FROM leads ORDER BY workflow_id, batch_id"
    ).fetchall()

    # group by (workflow_id, batch_id)
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"yet_to_reach_out": 0, "in_progress_telephony_failed": 0,
                 "in_progress_dialed": 0, "completed": 0, "total": 0}
    )

    for lead in leads:
        key = (lead["workflow_id"], lead["batch_id"])
        calls = _get_calls_for_lead(conn, lead["id"])
        bkt = _bucket(calls, conn)
        groups[key][bkt] += 1
        groups[key]["total"] += 1

    path = out_dir / "batch_summary.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "workflow_id", "batch_id", "total_leads",
            "yet_to_reach_out", "in_progress_telephony_failed",
            "in_progress_dialed", "completed",
        ])
        for (wf, bid), counts in sorted(groups.items()):
            writer.writerow([
                wf, bid, counts["total"],
                counts["yet_to_reach_out"],
                counts["in_progress_telephony_failed"],
                counts["in_progress_dialed"],
                counts["completed"],
            ])


# ── 2. daily_overview.csv ─────────────────────────────────────────────────────

def report_daily_overview(conn: sqlite3.Connection, out_dir: Path):
    # All call.initiated events that are linked
    initiated_rows = conn.execute(
        """SELECT e.call_id, e.occurred_at, e.lead_id,
                  l.workflow_id
           FROM events e
           JOIN leads l ON e.lead_id = l.id
           WHERE e.event_type='call.initiated' AND e.is_orphan=0"""
    ).fetchall()

    # {(workflow_id, ist_date): {total, connected, success_leads: set}}
    daily: dict[tuple, dict] = defaultdict(
        lambda: {"total": 0, "connected": 0, "success_leads": set()}
    )

    for row in initiated_rows:
        dt = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        date_str = ist_date_str(dt)
        key = (row["workflow_id"], date_str)
        daily[key]["total"] += 1

        call_id = row["call_id"]

        connected = conn.execute(
            "SELECT 1 FROM events WHERE call_id=? AND event_type='call.connected' AND is_orphan=0",
            (call_id,),
        ).fetchone()
        if connected:
            daily[key]["connected"] += 1

        # success on this date?
        code, _, is_success = _effective_disposition(conn, call_id)
        if is_success:
            daily[key]["success_leads"].add(row["lead_id"])

    path = out_dir / "daily_overview.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "workflow_id", "date", "total_calls", "connected_calls",
            "connectivity_rate", "success_leads",
        ])
        for (wf, date), d in sorted(daily.items()):
            rate = d["connected"] / d["total"] if d["total"] else 0
            writer.writerow([
                wf, date, d["total"], d["connected"],
                f"{rate:.4f}",
                len(d["success_leads"]),
            ])


# ── 3. lead_report.csv ────────────────────────────────────────────────────────

def report_lead_report(conn: sqlite3.Connection, out_dir: Path):
    leads = conn.execute(
        """SELECT id, workflow_id, external_ref, batch_id, phone_masked
           FROM leads ORDER BY workflow_id, external_ref"""
    ).fetchall()

    path = out_dir / "lead_report.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "workflow_id", "external_ref", "batch_id", "phone_masked",
            "bucket", "total_calls", "connected_calls",
            "last_call_at", "current_disposition", "ever_success",
        ])

        for lead in leads:
            calls = _get_calls_for_lead(conn, lead["id"])

            bkt = _bucket(calls, conn)
            total_calls = len(calls)
            connected_calls = sum(1 for c in calls if c["connected"])

            last_call_at = ""
            current_disp = ""
            ever_success = False

            if calls:
                latest_call = max(calls, key=lambda c: c["initiated_at_utc"])
                dt = datetime.fromisoformat(
                    latest_call["initiated_at_utc"].replace("Z", "+00:00")
                )
                last_call_at = ist_iso(dt)

                # current_disposition: effective disposition of most recent call that has one
                for c in sorted(calls, key=lambda x: x["initiated_at_utc"], reverse=True):
                    code, _, _ = _effective_disposition(conn, c["call_id"])
                    if code:
                        current_disp = code
                        break

                for c in calls:
                    _, _, is_success = _effective_disposition(conn, c["call_id"])
                    if is_success:
                        ever_success = True
                        break

            writer.writerow([
                lead["workflow_id"],
                lead["external_ref"],
                lead["batch_id"],
                lead["phone_masked"],
                bkt,
                total_calls,
                connected_calls,
                last_call_at,
                current_disp,
                "true" if ever_success else "false",
            ])


# ── 4. rejections.csv ─────────────────────────────────────────────────────────

def report_rejections(conn: sqlite3.Connection, out_dir: Path):
    rows = conn.execute(
        """SELECT workflow_id, batch_id, row_number, reason
           FROM rejections
           ORDER BY workflow_id, batch_id, row_number"""
    ).fetchall()

    path = out_dir / "rejections.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        for row in rows:
            writer.writerow([row["workflow_id"], row["batch_id"],
                             row["row_number"], row["reason"]])


# ── 5. data_quality.json ──────────────────────────────────────────────────────

def report_data_quality(conn: sqlite3.Connection, out_dir: Path):
    dq = conn.execute("SELECT * FROM data_quality WHERE id=1").fetchone()

    # Build rejections_by_reason
    reasons = conn.execute(
        "SELECT reason, count FROM rejection_reasons"
    ).fetchall()
    reasons_dict = {r["reason"]: r["count"] for r in reasons}

    result = {
        "lead_rows_total": dq["lead_rows_total"],
        "lead_rows_rejected": dq["lead_rows_rejected"],
        "rejections_by_reason": reasons_dict,
        "duplicate_lead_rows_merged": dq["duplicate_lead_rows_merged"],
        "field_warnings": dq["field_warnings"],
        "events_total": dq["events_total"],
        "malformed_events": dq["malformed_events"],
        "duplicate_events_dropped": dq["duplicate_events_dropped"],
        "orphan_events": dq["orphan_events"],
        "unknown_disposition_events": dq["unknown_disposition_events"],
    }

    path = out_dir / "data_quality.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")


# ── Main report entry point ───────────────────────────────────────────────────

def generate_reports(
    conn: sqlite3.Connection,
    out_dir: Path,
    disps_path: Path,
):
    out_dir.mkdir(parents=True, exist_ok=True)
    _ensure_disposition_catalogue(conn, disps_path)

    report_batch_summary(conn, out_dir)
    report_daily_overview(conn, out_dir)
    report_lead_report(conn, out_dir)
    report_rejections(conn, out_dir)
    report_data_quality(conn, out_dir)

