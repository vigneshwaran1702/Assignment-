"""
Lead ingestion — reads CSVs, validates/normalises each row, writes to DB.

Spec rules implemented:
- Header normalisation + alias matching
- Blank rows skipped (not counted)
- Row numbers count non-blank data rows from 1; header excluded
- Field validation in schema order; first failure = rejection reason
- Phone rejection: invalid_phone before missing_required:phone (spec §Lead files)
- Optional-field-present-but-invalid → field warning (not rejection)
- Lead identity = (workflow_id, canonical_phone)
- Duplicate merging: last-write-wins on non-null fields
- Defaults applied after all merging
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from .db import hash_pii, mask_phone
from .normalise import (
    normalise_header, normalise_phone,
    normalise_date, normalise_decimal, normalise_enum,
)
from .schema_loader import WorkflowSchema, FieldDef


# ── Header mapping ─────────────────────────────────────────────────────────────

def build_header_map(
    csv_headers: list[str], schema: WorkflowSchema
) -> dict[str, FieldDef]:
    """
    Map original CSV column names → FieldDef.
    Matching is done on normalised names (lower, trim, collapse _/whitespace).
    Unknown columns are silently ignored.
    """
    name_to_field: dict[str, FieldDef] = {}
    for fdef in schema.fields:
        name_to_field[normalise_header(fdef.name)] = fdef
        for alias in fdef.aliases:
            name_to_field[normalise_header(alias)] = fdef

    mapping: dict[str, FieldDef] = {}
    for col in csv_headers:
        norm = normalise_header(col)
        if norm in name_to_field:
            mapping[col] = name_to_field[norm]
    return mapping


# ── Single-field parsing ───────────────────────────────────────────────────────

def parse_field(raw: str, fdef: FieldDef) -> tuple[Any, bool]:
    """
    Parse a trimmed raw string according to field type.
    Returns (value, is_valid).
    Empty raw → (None, True) — "missing" is not the same as "invalid".
    """
    if raw == "":
        return None, True  # presence/required check is done in validate_row

    if fdef.type == "string":
        return raw, True

    if fdef.type == "phone":
        canon = normalise_phone(raw)
        return canon, canon is not None

    if fdef.type == "date":
        d = normalise_date(raw)
        return d, d is not None

    if fdef.type == "decimal":
        d = normalise_decimal(raw)
        return d, d is not None

    if fdef.type == "enum":
        v = normalise_enum(raw, fdef.values)
        return v, v is not None

    # Unknown type — pass through
    return raw, True


# ── Blank row detection ────────────────────────────────────────────────────────

def is_blank_row(row: dict[str, str]) -> bool:
    return all(v.strip() == "" for v in row.values())


# ── Row-level validation ───────────────────────────────────────────────────────

def validate_row(
    raw_row: dict[str, str],
    header_map: dict[str, FieldDef],
    schema: WorkflowSchema,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """
    Validate and normalise one CSV row.

    Returns:
        (parsed_dict, rejection_reason, field_warnings)
        parsed_dict is None when the row is rejected.

    Rejection-reason priority (spec §Lead files, in schema-field order):
        1. missing_required:<field_name>  — required field absent
        2. invalid_phone                  — phone present but not a valid Indian mobile

    For phone specifically: invalid_phone takes precedence over
    missing_required:phone when the raw value is non-empty (spec is explicit).

    Field warnings are counted for optional fields that are present but invalid;
    their parsed value is set to None.
    """
    # ── Step 1: parse every mapped column ─────────────────────────────────────
    # field_name → (raw_trimmed, parsed_value, is_valid)
    field_data: dict[str, tuple[str, Any, bool]] = {}

    for col, raw_val in raw_row.items():
        fdef = header_map.get(col)
        if fdef is None:
            continue
        raw_trimmed = raw_val.strip()
        value, ok = parse_field(raw_trimmed, fdef)
        field_data[fdef.name] = (raw_trimmed, value, ok)

    # ── Step 2: walk fields in schema order, detect rejection ──────────────────
    parsed_out: dict[str, Any] = {}
    warnings = 0

    for fdef in schema.fields:
        raw_trimmed, value, ok = field_data.get(fdef.name, ("", None, True))

        if fdef.type == "phone":
            # Spec: "phone is present but invalid → invalid_phone"
            # This takes precedence over "missing_required" for the phone field.
            if raw_trimmed and value is None:
                # Present but failed normalisation
                return None, "invalid_phone", 0
            if fdef.required and not raw_trimmed:
                # Empty and required
                return None, f"missing_required:{fdef.name}", 0
        else:
            # All other field types
            if fdef.required and value is None:
                return None, f"missing_required:{fdef.name}", 0

            # Optional field present but invalid → warning
            if not fdef.required and raw_trimmed and not ok:
                value = None   # store null
                warnings += 1

        parsed_out[fdef.name] = value

    return parsed_out, None, warnings


# ── Main ingest function ───────────────────────────────────────────────────────

def ingest_leads(
    conn: sqlite3.Connection,
    csv_path: Path,
    workflow_id: str,
    batch_id: str,
    schema: WorkflowSchema,
) -> dict:
    """
    Ingest one CSV lead file against the workflow schema.
    Idempotent: skips the batch if it has already been processed.
    Returns a stats dict.
    """
    # Idempotency guard at batch level
    if conn.execute(
        "SELECT 1 FROM ingested_batches WHERE workflow_id=? AND batch_id=?",
        (workflow_id, batch_id),
    ).fetchone():
        return {"skipped": True}

    stats = {
        "total": 0, "rejected": 0, "new_leads": 0,
        "merged": 0, "warnings": 0,
    }

    ref_field = schema.reference_field()
    new_lead_ids: list[int] = []  # track new leads to apply defaults only to them

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header_map = build_header_map(list(reader.fieldnames or []), schema)

        row_number = 0
        for raw_row in reader:
            if is_blank_row(raw_row):
                continue        # skipped, not counted
            row_number += 1
            stats["total"] += 1

            parsed, reason, warnings = validate_row(raw_row, header_map, schema)

            if reason:
                # Row rejected
                stats["rejected"] += 1
                conn.execute(
                    """INSERT OR IGNORE INTO rejections
                       (workflow_id, batch_id, row_number, reason)
                       VALUES (?,?,?,?)""",
                    (workflow_id, batch_id, row_number, reason),
                )
                conn.execute(
                    """INSERT INTO rejection_reasons (reason, count) VALUES (?,1)
                       ON CONFLICT(reason) DO UPDATE SET count=count+1""",
                    (reason,),
                )
                continue

            stats["warnings"] += warnings

            phone_canon   = parsed["phone"]           # already a canonical +91XXXXXXXXXX
            phone_hash    = hash_pii(phone_canon)
            phone_masked  = mask_phone(phone_canon)
            name_hash     = hash_pii(parsed.get("customer_name") or "")
            external_ref  = parsed.get(ref_field.name, "") if ref_field else ""

            existing_lead = conn.execute(
                "SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?",
                (workflow_id, phone_hash),
            ).fetchone()

            if existing_lead is None:
                # ── New lead ───────────────────────────────────────────────────
                cur = conn.execute(
                    """INSERT INTO leads
                       (workflow_id, phone_hash, phone_masked, external_ref,
                        batch_id, customer_name_hash,
                        policy_expiry, premium_amount, preferred_language,
                        product, order_date)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workflow_id, phone_hash, phone_masked,
                        external_ref or "", batch_id, name_hash,
                        parsed.get("policy_expiry"),
                        float(parsed["premium_amount"])
                            if parsed.get("premium_amount") is not None else None,
                        parsed.get("preferred_language"),
                        parsed.get("product"),
                        parsed.get("order_date"),
                    ),
                )
                stats["new_leads"] += 1
                new_lead_ids.append(cur.lastrowid)
            else:
                # ── Duplicate — overwrite non-null fields (last-write-wins) ───
                stats["merged"] += 1
                lead_id = existing_lead["id"]
                updates: list[str] = []
                params: list[Any] = []

                def _set(col: str, val: Any) -> None:
                    if val is not None:
                        updates.append(f"{col}=?")
                        params.append(val)

                _set("customer_name_hash",
                     name_hash if parsed.get("customer_name") else None)
                _set("policy_expiry",    parsed.get("policy_expiry"))
                _set("preferred_language", parsed.get("preferred_language"))
                _set("product",          parsed.get("product"))
                _set("order_date",       parsed.get("order_date"))

                if parsed.get("premium_amount") is not None:
                    updates.append("premium_amount=?")
                    params.append(float(parsed["premium_amount"]))

                if external_ref:
                    updates.append("external_ref=?")
                    params.append(external_ref)

                if updates:
                    params.append(lead_id)
                    conn.execute(
                        f"UPDATE leads SET {', '.join(updates)} WHERE id=?",
                        params,
                    )

    # ── Apply defaults to NEW leads that still have null preferred_language ────
    # (Spec: "Defaults apply after merging, to fields that are still null.")
    # We apply only to leads created in this batch run so we don't clobber
    # other workflows' leads or leads that already have a value.
    if new_lead_ids:
        placeholders = ",".join("?" * len(new_lead_ids))
        conn.execute(
            f"""UPDATE leads SET preferred_language='en'
                WHERE preferred_language IS NULL
                  AND id IN ({placeholders})""",
            new_lead_ids,
        )

    # Mark batch done (idempotency)
    conn.execute(
        "INSERT OR IGNORE INTO ingested_batches (workflow_id, batch_id, file_path) VALUES (?,?,?)",
        (workflow_id, batch_id, str(csv_path)),
    )

    # Persist running quality counters
    conn.execute(
        """UPDATE data_quality SET
           lead_rows_total             = lead_rows_total + ?,
           lead_rows_rejected          = lead_rows_rejected + ?,
           duplicate_lead_rows_merged  = duplicate_lead_rows_merged + ?,
           field_warnings              = field_warnings + ?
           WHERE id=1""",
        (stats["total"], stats["rejected"], stats["merged"], stats["warnings"]),
    )
    conn.commit()
    return stats
