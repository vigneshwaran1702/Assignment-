"""
Event ingestion — reads JSONL files into the append-only interaction ledger.

Spec processing order (strictly enforced):
  1. Malformed  — not valid JSON, or no event_id  → count + skip
  2. Duplicate  — event_id already seen            → count + skip
  3. Orphan     — can't link to a lead             → store with is_orphan=1
  4. Unknown disposition — disposition.* with code not in catalogue
                           (only reached if event IS linked)  → count + skip

After every drop, relink_orphans() re-attempts to link previously-orphaned
events whose lead has now arrived.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .db import hash_pii
from .normalise import normalise_phone, parse_event_timestamp
from .schema_loader import Disposition


# ── Helpers ────────────────────────────────────────────────────────────────────

def _phone_hash_from_event(event: dict) -> str | None:
    raw = event.get("phone", "")
    if not raw:
        return None
    canon = normalise_phone(str(raw))
    return hash_pii(canon) if canon else None


def _resolve_lead_id(
    conn: sqlite3.Connection,
    event: dict,
    workflow_id: str | None,
) -> int | None:
    """
    Try to link this event to a lead.

    Normal events  → (workflow_id, phone_hash)
    disposition.override → has no phone; link via call_id to the lead of
                           any other event on that call.
    """
    etype = event.get("event_type", "")

    if etype == "disposition.override":
        call_id = event.get("call_id")
        if not call_id:
            return None
        row = conn.execute(
            "SELECT lead_id FROM events WHERE call_id=? AND lead_id IS NOT NULL LIMIT 1",
            (call_id,),
        ).fetchone()
        return row["lead_id"] if row else None

    ph_hash = _phone_hash_from_event(event)
    if not ph_hash or not workflow_id:
        return None

    row = conn.execute(
        "SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?",
        (workflow_id, ph_hash),
    ).fetchone()
    return row["id"] if row else None


# ── Main ingest function ───────────────────────────────────────────────────────

def ingest_events(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    dispositions: dict[str, dict[str, Disposition]],
    drop_sequence: int,
) -> dict:
    """
    Ingest one JSONL events file into the append-only ledger.
    Returns stats dict.
    """
    stats = {
        "total": 0, "malformed": 0, "duplicate": 0,
        "orphan": 0, "unknown_disposition": 0, "inserted": 0,
    }

    with open(jsonl_path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue

            # events_total counts every non-empty line (spec §data_quality.json)
            stats["total"] += 1

            # ── Step 1: Malformed ──────────────────────────────────────────────
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue

            if not isinstance(event, dict) or "event_id" not in event:
                stats["malformed"] += 1
                continue

            event_id    = event["event_id"]
            workflow_id = event.get("workflow_id")
            call_id     = event.get("call_id", "")
            etype       = event.get("event_type", "")
            payload_raw = event.get("payload", {})
            occurred_at_raw = event.get("occurred_at", "")

            # ── Step 2: Duplicate ──────────────────────────────────────────────
            if conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone():
                stats["duplicate"] += 1
                continue

            # Parse timestamp (malformed if unparseable)
            try:
                occurred_dt     = parse_event_timestamp(occurred_at_raw)
                occurred_at_utc = occurred_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                stats["malformed"] += 1
                continue

            payload = json.dumps(payload_raw)

            # ── Step 3: Orphan ─────────────────────────────────────────────────
            # Must come BEFORE unknown-disposition check (spec §Event files).
            lead_id = _resolve_lead_id(conn, event, workflow_id)

            if lead_id is None:
                # Can't link → store as orphan; will be re-linked later
                stats["orphan"] += 1
                conn.execute(
                    """INSERT OR IGNORE INTO events
                       (event_id, event_type, workflow_id, call_id,
                        occurred_at, lead_id, payload, is_orphan, drop_sequence)
                       VALUES (?,?,?,?,?,?,?,1,?)""",
                    (event_id, etype, workflow_id, call_id,
                     occurred_at_utc, None, payload, drop_sequence),
                )
                stats["inserted"] += 1
                continue

            # ── Step 4: Unknown disposition ────────────────────────────────────
            # Only reached when the event IS linked (lead_id is not None).
            if etype in ("disposition.set", "disposition.override"):
                disp_code = payload_raw.get("disposition_code") if isinstance(payload_raw, dict) else None
                wf_disps  = dispositions.get(workflow_id or "", {})
                if disp_code and disp_code not in wf_disps:
                    stats["unknown_disposition"] += 1
                    continue   # do NOT insert; don't add to ledger

            # ── Insert linked event ────────────────────────────────────────────
            conn.execute(
                """INSERT OR IGNORE INTO events
                   (event_id, event_type, workflow_id, call_id,
                    occurred_at, lead_id, payload, is_orphan, drop_sequence)
                   VALUES (?,?,?,?,?,?,?,0,?)""",
                (event_id, etype, workflow_id, call_id,
                 occurred_at_utc, lead_id, payload, drop_sequence),
            )
            stats["inserted"] += 1

    conn.commit()

    # Persist running quality counters
    conn.execute(
        """UPDATE data_quality SET
           events_total                = events_total + ?,
           malformed_events            = malformed_events + ?,
           duplicate_events_dropped    = duplicate_events_dropped + ?,
           unknown_disposition_events  = unknown_disposition_events + ?
           WHERE id=1""",
        (stats["total"], stats["malformed"],
         stats["duplicate"], stats["unknown_disposition"]),
    )
    conn.commit()
    return stats


# ── Orphan re-linkage ──────────────────────────────────────────────────────────

def relink_orphans(
    conn: sqlite3.Connection,
    dispositions: dict[str, dict[str, Disposition]],
) -> int:
    """
    After a drop is fully ingested (leads then events), re-attempt to link
    all orphaned events whose lead may now exist.

    Spec: "An event that was an orphan after drop_1 must count once its lead
    arrives in drop_2."

    For disposition events that now link but have an unknown code: they are
    removed from the ledger and counted as unknown_disposition instead of orphan.

    Returns the number of events newly linked.
    """
    orphans = conn.execute(
        """SELECT id, event_id, event_type, workflow_id, call_id, payload
           FROM events WHERE is_orphan=1"""
    ).fetchall()

    linked = 0
    unknown_dispositions_found = 0

    for row in orphans:
        event = {
            "event_id":   row["event_id"],
            "event_type": row["event_type"],
            "workflow_id":row["workflow_id"],
            "call_id":    row["call_id"],
            "payload":    json.loads(row["payload"]),
        }

        lead_id = _resolve_lead_id(conn, event, row["workflow_id"])
        if lead_id is None:
            continue    # still an orphan; leave it alone

        # Event now links. Apply step 4 (unknown-disposition check) now.
        etype = row["event_type"]
        if etype in ("disposition.set", "disposition.override"):
            payload = json.loads(row["payload"])
            disp_code = payload.get("disposition_code")
            wf_disps  = dispositions.get(row["workflow_id"] or "", {})
            if disp_code and disp_code not in wf_disps:
                # Remove from ledger; re-classify as unknown_disposition
                conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
                unknown_dispositions_found += 1
                continue

        # Successfully re-link
        conn.execute(
            "UPDATE events SET lead_id=?, is_orphan=0 WHERE id=?",
            (lead_id, row["id"]),
        )
        linked += 1

    if unknown_dispositions_found:
        conn.execute(
            """UPDATE data_quality
               SET unknown_disposition_events = unknown_disposition_events + ?
               WHERE id=1""",
            (unknown_dispositions_found,),
        )

    conn.commit()

    # Recompute final orphan count from the actual ledger state
    final_orphan_count = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE is_orphan=1"
    ).fetchone()["c"]
    conn.execute(
        "UPDATE data_quality SET orphan_events=? WHERE id=1",
        (final_orphan_count,),
    )
    conn.commit()
    return linked
