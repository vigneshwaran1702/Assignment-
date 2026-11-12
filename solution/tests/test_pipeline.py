"""
Unit tests for the rules most likely to break:
  - Phone normalisation (all 6+ formats)
  - Date parsing (all 4 formats + impossible dates)
  - Decimal parsing (all currency prefixes)
  - Effective disposition logic
  - Bucket logic
  - Header mapping
  - Orphan re-linking
  - Idempotency (re-ingest doesn't double-count)
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import csv
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from solution.pipeline.normalise import (
    normalise_phone, normalise_date, normalise_decimal,
    normalise_header, normalise_enum, parse_event_timestamp, ist_iso,
)
from solution.pipeline.db import init_db, hash_pii, mask_phone
from solution.pipeline.schema_loader import load_schema, load_dispositions


# ═══════════════════════════════════════════════════════════════════════════════
# Phone normalisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhoneNormalisation:
    def test_plus91_no_spaces(self):
        assert normalise_phone("+916463745499") == "+916463745499"

    def test_10_digits_valid(self):
        assert normalise_phone("9932302346") == "+919932302346"

    def test_plus91_with_spaces(self):
        assert normalise_phone("+91 79328 96340") == "+917932896340"

    def test_parentheses_format(self):
        assert normalise_phone("(+91) 8196106462") == "+918196106462"

    def test_91_dashes(self):
        assert normalise_phone("91-77187-50036") == "+917718750036"

    def test_leading_zero_11_digits(self):
        assert normalise_phone("06832582386") == "+916832582386"

    def test_invalid_starts_with_5(self):
        assert normalise_phone("5123456789") is None

    def test_too_short(self):
        assert normalise_phone("98765") is None

    def test_empty(self):
        assert normalise_phone("") is None

    def test_valid_start_6(self):
        assert normalise_phone("6000000000") == "+916000000000"

    def test_valid_start_7(self):
        assert normalise_phone("7000000000") == "+917000000000"

    def test_12_digits_no_91_prefix(self):
        # 12 digits not starting with 91 → invalid
        assert normalise_phone("821234567890") is None

    def test_plus91_12_digits(self):
        assert normalise_phone("919932302346") == "+919932302346"


# ═══════════════════════════════════════════════════════════════════════════════
# Date parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestDateParsing:
    def test_yyyy_mm_dd(self):
        assert normalise_date("2026-11-23") == "2026-11-23"

    def test_dd_slash_mm_yyyy(self):
        assert normalise_date("27/10/2026") == "2026-10-27"

    def test_dd_dash_mm_yyyy(self):
        assert normalise_date("17-12-2026") == "2026-12-17"

    def test_dd_mon_yyyy_lower(self):
        assert normalise_date("22-sep-2026") == "2026-09-22"

    def test_dd_mon_yyyy_mixed(self):
        assert normalise_date("01-Dec-2026") == "2026-12-01"

    def test_impossible_date(self):
        assert normalise_date("31-02-2026") is None

    def test_impossible_date_yyyy(self):
        assert normalise_date("2026-13-01") is None

    def test_empty(self):
        assert normalise_date("") is None

    def test_invalid_month_name(self):
        assert normalise_date("01-Xyz-2026") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Decimal parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestDecimalParsing:
    def test_plain(self):
        from decimal import Decimal
        assert normalise_decimal("6850") == Decimal("6850.00")

    def test_inr_prefix(self):
        from decimal import Decimal
        assert normalise_decimal("INR 25301.4") == Decimal("25301.40")

    def test_rs_prefix(self):
        from decimal import Decimal
        assert normalise_decimal("Rs. 31629") == Decimal("31629.00")

    def test_rupee_symbol_with_comma(self):
        from decimal import Decimal
        # ₹27,554
        assert normalise_decimal("₹27,554") == Decimal("27554.00")

    def test_negative(self):
        assert normalise_decimal("-100") is None

    def test_empty(self):
        assert normalise_decimal("") is None

    def test_zero(self):
        from decimal import Decimal
        assert normalise_decimal("0") == Decimal("0.00")


# ═══════════════════════════════════════════════════════════════════════════════
# Enum normalisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnumNormalisation:
    VALUES = ["en", "hi", "ta", "kn", "te", "mr"]

    def test_uppercase_match(self):
        assert normalise_enum("HI", self.VALUES) == "hi"

    def test_mixed_case(self):
        assert normalise_enum("TA", self.VALUES) == "ta"

    def test_valid_lower(self):
        assert normalise_enum("en", self.VALUES) == "en"

    def test_invalid_value(self):
        # "Hindi" is not in the values list
        assert normalise_enum("Hindi", self.VALUES) is None

    def test_with_spaces(self):
        # trimmed before matching
        assert normalise_enum(" kn ", self.VALUES) == "kn"


# ═══════════════════════════════════════════════════════════════════════════════
# Header normalisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeaderNormalisation:
    def test_underscores_to_space(self):
        assert normalise_header("customer_name") == "customer name"

    def test_mixed_whitespace(self):
        assert normalise_header("  Mobile  No  ") == "mobile no"

    def test_already_normalised(self):
        assert normalise_header("phone") == "phone"

    def test_capitals(self):
        assert normalise_header("Policy_Number") == "policy number"


# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimestampParsing:
    def test_utc_z(self):
        dt = parse_event_timestamp("2026-10-05T13:20:21Z")
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 13

    def test_offset(self):
        dt = parse_event_timestamp("2026-10-05T13:20:21+05:30")
        # Should be converted to UTC: 13:20:21 IST = 07:50:21 UTC
        utc = dt.astimezone(timezone.utc)
        assert utc.hour == 7
        assert utc.minute == 50

    def test_naive_treated_as_ist(self):
        dt = parse_event_timestamp("2026-10-03 10:01:00")
        # 10:01 IST = 04:31 UTC
        utc = dt.astimezone(timezone.utc)
        assert utc.hour == 4
        assert utc.minute == 31


# ═══════════════════════════════════════════════════════════════════════════════
# Database: mask_phone, hash_pii
# ═══════════════════════════════════════════════════════════════════════════════

class TestPII:
    def test_mask_phone(self):
        assert mask_phone("+916463745499") == "+91******5499"

    def test_hash_deterministic(self):
        assert hash_pii("test") == hash_pii("test")

    def test_hash_different_values(self):
        assert hash_pii("+919876543210") != hash_pii("+919876543211")


# ═══════════════════════════════════════════════════════════════════════════════
# Effective disposition and bucket logic (integration)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(db_path)
    # Seed disposition catalogue
    conn.execute(
        """CREATE TABLE IF NOT EXISTS disposition_catalogue (
               workflow_id TEXT, code TEXT, is_terminal INTEGER, is_success INTEGER,
               PRIMARY KEY (workflow_id, code))"""
    )
    for code, is_terminal, is_success in [
        ("renewed", 1, 1),
        ("callback_requested", 0, 0),
        ("not_interested", 1, 0),
        ("dnd", 1, 0),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO disposition_catalogue VALUES (?,?,?,?)",
            ("motor_renewal", code, is_terminal, is_success),
        )
    conn.commit()
    return conn


def _add_lead(conn, workflow_id="motor_renewal", phone="+919876543210"):
    ph_hash = hash_pii(phone)
    ph_masked = mask_phone(phone)
    conn.execute(
        """INSERT OR IGNORE INTO leads
           (workflow_id, phone_hash, phone_masked, external_ref, batch_id, customer_name_hash)
           VALUES (?,?,?,?,?,?)""",
        (workflow_id, ph_hash, ph_masked, "POL001", "batch_001", hash_pii("Test User")),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM leads WHERE workflow_id=? AND phone_hash=?",
        (workflow_id, ph_hash),
    ).fetchone()["id"]


def _add_event(conn, event_id, event_type, call_id, lead_id,
               occurred_at="2026-10-01T10:00:00Z", payload=None, workflow_id="motor_renewal"):
    conn.execute(
        """INSERT INTO events
           (event_id, event_type, workflow_id, call_id, occurred_at, lead_id, payload, is_orphan)
           VALUES (?,?,?,?,?,?,?,0)""",
        (event_id, event_type, workflow_id, call_id, occurred_at,
         lead_id, json.dumps(payload or {})),
    )
    conn.commit()


class TestEffectiveDisposition:
    def test_no_disposition(self, fresh_db):
        from solution.pipeline.report import _effective_disposition
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        code, is_terminal, is_success = _effective_disposition(fresh_db, "call_A")
        assert code is None
        assert not is_terminal
        assert not is_success

    def test_disposition_set(self, fresh_db):
        from solution.pipeline.report import _effective_disposition
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        _add_event(fresh_db, "e2", "disposition.set", "call_A", lead_id,
                   payload={"disposition_code": "renewed"})
        code, is_terminal, is_success = _effective_disposition(fresh_db, "call_A")
        assert code == "renewed"
        assert is_terminal
        assert is_success

    def test_override_beats_set(self, fresh_db):
        from solution.pipeline.report import _effective_disposition
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id,
                   occurred_at="2026-10-01T09:00:00Z")
        _add_event(fresh_db, "e2", "disposition.set", "call_A", lead_id,
                   occurred_at="2026-10-01T09:05:00Z",
                   payload={"disposition_code": "callback_requested"})
        _add_event(fresh_db, "e3", "disposition.override", "call_A", lead_id,
                   occurred_at="2026-10-01T09:10:00Z",
                   payload={"disposition_code": "not_interested",
                            "overridden_by": "supervisor", "reason": "wrong"})
        code, is_terminal, _ = _effective_disposition(fresh_db, "call_A")
        assert code == "not_interested"
        assert is_terminal

    def test_latest_override_wins(self, fresh_db):
        from solution.pipeline.report import _effective_disposition
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        _add_event(fresh_db, "e2", "disposition.override", "call_A", lead_id,
                   occurred_at="2026-10-01T09:00:00Z",
                   payload={"disposition_code": "not_interested"})
        _add_event(fresh_db, "e3", "disposition.override", "call_A", lead_id,
                   occurred_at="2026-10-01T10:00:00Z",
                   payload={"disposition_code": "renewed"})
        code, _, is_success = _effective_disposition(fresh_db, "call_A")
        assert code == "renewed"
        assert is_success


class TestBucketLogic:
    def test_no_calls(self, fresh_db):
        from solution.pipeline.report import _bucket, _get_calls_for_lead
        lead_id = _add_lead(fresh_db)
        calls = _get_calls_for_lead(fresh_db, lead_id)
        assert _bucket(calls, fresh_db) == "yet_to_reach_out"

    def test_terminal_disposition_completed(self, fresh_db):
        from solution.pipeline.report import _bucket, _get_calls_for_lead
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        _add_event(fresh_db, "e2", "disposition.set", "call_A", lead_id,
                   payload={"disposition_code": "renewed"})
        calls = _get_calls_for_lead(fresh_db, lead_id)
        assert _bucket(calls, fresh_db) == "completed"

    def test_all_telephony_failed(self, fresh_db):
        from solution.pipeline.report import _bucket, _get_calls_for_lead
        lead_id = _add_lead(fresh_db)
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        _add_event(fresh_db, "e2", "call.telephony_failed", "call_A", lead_id)
        calls = _get_calls_for_lead(fresh_db, lead_id)
        assert _bucket(calls, fresh_db) == "in_progress_telephony_failed"

    def test_mixed_calls_in_progress_dialed(self, fresh_db):
        from solution.pipeline.report import _bucket, _get_calls_for_lead
        lead_id = _add_lead(fresh_db)
        # call_A: telephony failed
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id,
                   occurred_at="2026-10-01T09:00:00Z")
        _add_event(fresh_db, "e2", "call.telephony_failed", "call_A", lead_id)
        # call_B: no telephony failed (dialed)
        _add_event(fresh_db, "e3", "call.initiated", "call_B", lead_id,
                   occurred_at="2026-10-01T10:00:00Z")
        calls = _get_calls_for_lead(fresh_db, lead_id)
        assert _bucket(calls, fresh_db) == "in_progress_dialed"

    def test_completed_takes_priority_over_telephony_failed(self, fresh_db):
        from solution.pipeline.report import _bucket, _get_calls_for_lead
        lead_id = _add_lead(fresh_db)
        # call_A: terminal disposition
        _add_event(fresh_db, "e1", "call.initiated", "call_A", lead_id)
        _add_event(fresh_db, "e2", "disposition.set", "call_A", lead_id,
                   payload={"disposition_code": "dnd"})
        # call_B: telephony failed
        _add_event(fresh_db, "e3", "call.initiated", "call_B", lead_id,
                   occurred_at="2026-10-01T11:00:00Z")
        _add_event(fresh_db, "e4", "call.telephony_failed", "call_B", lead_id)
        calls = _get_calls_for_lead(fresh_db, lead_id)
        assert _bucket(calls, fresh_db) == "completed"


# ═══════════════════════════════════════════════════════════════════════════════
# Idempotency: re-ingesting the same batch changes nothing
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Validation ordering — invalid_phone vs missing_required (Bug 1 fix)
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidationOrdering:
    """
    Spec: walk fields in schema order. For phone specifically,
    invalid_phone must fire BEFORE missing_required:phone when the
    raw value is present but invalid.
    """

    def _make_schema(self, tmp_path):
        import json
        from solution.pipeline.schema_loader import load_schema
        p = tmp_path / "motor_renewal.json"
        p.write_text(json.dumps({
            "workflow_id": "motor_renewal",
            "description": "test",
            "fields": [
                {"name": "customer_name", "type": "string",  "required": True,  "pii": True,  "aliases": []},
                {"name": "phone",         "type": "phone",   "required": True,  "pii": True,  "aliases": []},
                {"name": "policy_number", "type": "string",  "required": True,  "reference": True, "aliases": []},
                {"name": "policy_expiry", "type": "date",    "required": False, "aliases": []},
                {"name": "preferred_language", "type": "enum", "required": False,
                 "values": ["en", "hi"], "default": "en", "aliases": []},
            ],
        }))
        return load_schema(p)

    def test_invalid_phone_beats_missing_required_phone(self, tmp_path):
        """Present-but-invalid phone → invalid_phone, NOT missing_required:phone."""
        from solution.pipeline.ingest_leads import validate_row, build_header_map
        schema = self._make_schema(tmp_path)
        raw_row = {
            "customer_name": "Test User",
            "phone": "12345",           # too short → invalid
            "policy_number": "POL001",
        }
        header_map = build_header_map(list(raw_row.keys()), schema)
        parsed, reason, _ = validate_row(raw_row, header_map, schema)
        assert parsed is None
        assert reason == "invalid_phone"

    def test_missing_phone_gives_missing_required(self, tmp_path):
        """Empty phone on a required field → missing_required:phone."""
        from solution.pipeline.ingest_leads import validate_row, build_header_map
        schema = self._make_schema(tmp_path)
        raw_row = {
            "customer_name": "Test User",
            "phone": "",                # empty → missing
            "policy_number": "POL001",
        }
        header_map = build_header_map(list(raw_row.keys()), schema)
        parsed, reason, _ = validate_row(raw_row, header_map, schema)
        assert parsed is None
        assert reason == "missing_required:phone"

    def test_missing_required_before_invalid_phone_in_schema_order(self, tmp_path):
        """customer_name is before phone in schema; missing name fires first."""
        from solution.pipeline.ingest_leads import validate_row, build_header_map
        schema = self._make_schema(tmp_path)
        raw_row = {
            "customer_name": "",        # missing required
            "phone": "0000000000",      # would also be invalid (starts with 0→9digits→too short)
            "policy_number": "POL001",
        }
        header_map = build_header_map(list(raw_row.keys()), schema)
        parsed, reason, _ = validate_row(raw_row, header_map, schema)
        assert parsed is None
        assert reason == "missing_required:customer_name"

    def test_optional_invalid_enum_is_warning_not_rejection(self, tmp_path):
        """Optional field present but invalid → field warning, row not rejected."""
        from solution.pipeline.ingest_leads import validate_row, build_header_map
        schema = self._make_schema(tmp_path)
        raw_row = {
            "customer_name": "Test User",
            "phone": "9876543210",
            "policy_number": "POL001",
            "preferred_language": "Hindi",  # not in enum values
        }
        header_map = build_header_map(list(raw_row.keys()), schema)
        parsed, reason, warnings = validate_row(raw_row, header_map, schema)
        assert reason is None           # not a rejection
        assert warnings == 1
        assert parsed["preferred_language"] is None  # stored null

    def test_optional_invalid_date_is_warning(self, tmp_path):
        """Optional date that is impossible → warning."""
        from solution.pipeline.ingest_leads import validate_row, build_header_map
        schema = self._make_schema(tmp_path)
        raw_row = {
            "customer_name": "Test User",
            "phone": "9876543210",
            "policy_number": "POL001",
            "policy_expiry": "31-02-2026",  # impossible date
        }
        header_map = build_header_map(list(raw_row.keys()), schema)
        parsed, reason, warnings = validate_row(raw_row, header_map, schema)
        assert reason is None
        assert warnings == 1
        assert parsed["policy_expiry"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# Event processing order — orphan before unknown_disposition (Bug 2 fix)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventProcessingOrder:
    """
    Spec: step 3 (orphan) before step 4 (unknown disposition).
    An event that can't be linked is an orphan even if its disposition
    code is not in the catalogue.
    """

    @pytest.fixture
    def db_with_disps(self, tmp_path):
        from solution.pipeline.db import init_db
        conn = init_db(tmp_path / "ev.db")
        return conn

    def _make_disps(self):
        from solution.pipeline.schema_loader import Disposition
        d = Disposition("motor_renewal", "renewed", "Renewed", True, True)
        return {"motor_renewal": {"renewed": d}}

    def test_linked_unknown_disposition_not_stored(self, tmp_path, db_with_disps):
        """A linked event with unknown disposition code → unknown_disp count, NOT in ledger."""
        import json, tempfile
        from solution.pipeline.db import hash_pii, mask_phone
        from solution.pipeline.ingest_events import ingest_events

        conn = db_with_disps
        # Insert a lead so the event can link
        conn.execute(
            """INSERT INTO leads
               (workflow_id, phone_hash, phone_masked, external_ref, batch_id, customer_name_hash)
               VALUES (?,?,?,?,?,?)""",
            ("motor_renewal", hash_pii("+919876543210"),
             mask_phone("+919876543210"), "POL001", "b001", "x"),
        )
        conn.commit()

        event_line = json.dumps({
            "event_id": "e_unk1",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_X",
            "occurred_at": "2026-10-01T10:00:00Z",
            "phone": "+919876543210",
            "payload": {"disposition_code": "UNKNOWN_CODE"},
        })
        jsonl = tmp_path / "ev.jsonl"
        jsonl.write_text(event_line + "\n")

        disps = self._make_disps()
        stats = ingest_events(conn, jsonl, disps, drop_sequence=1)

        assert stats["unknown_disposition"] == 1
        assert stats["orphan"] == 0
        # Must NOT be in the ledger
        row = conn.execute("SELECT 1 FROM events WHERE event_id='e_unk1'").fetchone()
        assert row is None

    def test_orphan_with_unknown_disposition_counts_as_orphan(self, tmp_path, db_with_disps):
        """
        An event that can't link (no matching lead) counts as ORPHAN,
        NOT as unknown_disposition — even if its disposition code is unknown.
        Spec step 3 (orphan) fires before step 4 (unknown disp).
        """
        import json
        from solution.pipeline.ingest_events import ingest_events

        conn = db_with_disps
        # No lead inserted → event will be orphan

        event_line = json.dumps({
            "event_id": "e_unk2",
            "event_type": "disposition.set",
            "workflow_id": "motor_renewal",
            "call_id": "call_Y",
            "occurred_at": "2026-10-01T10:00:00Z",
            "phone": "+919999999999",   # no lead with this phone
            "payload": {"disposition_code": "UNKNOWN_CODE"},
        })
        jsonl = tmp_path / "ev2.jsonl"
        jsonl.write_text(event_line + "\n")

        disps = self._make_disps()
        stats = ingest_events(conn, jsonl, disps, drop_sequence=1)

        assert stats["orphan"] == 1
        assert stats["unknown_disposition"] == 0
        # Stored in ledger as orphan
        row = conn.execute(
            "SELECT is_orphan FROM events WHERE event_id='e_unk2'"
        ).fetchone()
        assert row is not None
        assert row["is_orphan"] == 1


class TestIdempotency:
    def test_same_batch_twice(self, tmp_path):
        from solution.pipeline.db import init_db
        from solution.pipeline.schema_loader import load_schema
        from solution.pipeline.ingest_leads import ingest_leads

        # Create a minimal schema
        schema_path = tmp_path / "motor_renewal.json"
        schema_path.write_text(json.dumps({
            "workflow_id": "motor_renewal",
            "description": "test",
            "fields": [
                {"name": "customer_name", "type": "string", "required": True, "pii": True, "aliases": []},
                {"name": "phone", "type": "phone", "required": True, "pii": True, "aliases": []},
                {"name": "policy_number", "type": "string", "required": True, "reference": True, "aliases": []},
            ]
        }))
        schema = load_schema(schema_path)

        # Create CSV
        csv_path = tmp_path / "batch_001.csv"
        csv_path.write_text("customer_name,phone,policy_number\nTest User,9876543210,POL001\n")

        db_path = tmp_path / "test.db"
        conn = init_db(db_path)

        s1 = ingest_leads(conn, csv_path, "motor_renewal", "batch_001", schema)
        s2 = ingest_leads(conn, csv_path, "motor_renewal", "batch_001", schema)

        assert s1["new_leads"] == 1
        assert s2.get("skipped") is True

        # Only 1 lead in DB
        count = conn.execute("SELECT COUNT(*) as c FROM leads").fetchone()["c"]
        assert count == 1

