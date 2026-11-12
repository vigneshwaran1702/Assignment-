"""
Schema loader — reads JSON schema files and dispositions catalogue.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FieldDef:
    name: str
    type: str                       # string | phone | date | decimal | enum
    required: bool = False
    pii: bool = False
    reference: bool = False         # marks the external_ref field
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)   # for enum
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: list[FieldDef]

    def reference_field(self) -> FieldDef | None:
        for f in self.fields:
            if f.reference:
                return f
        return None


@dataclass
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def load_schema(path: Path) -> WorkflowSchema:
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = []
    for f in data["fields"]:
        fields.append(FieldDef(
            name=f["name"],
            type=f["type"],
            required=f.get("required", False),
            pii=f.get("pii", False),
            reference=f.get("reference", False),
            aliases=f.get("aliases", []),
            values=f.get("values", []),
            default=f.get("default"),
        ))
    return WorkflowSchema(
        workflow_id=data["workflow_id"],
        description=data["description"],
        fields=fields,
    )


def load_all_schemas(schemas_dir: Path) -> dict[str, WorkflowSchema]:
    schemas: dict[str, WorkflowSchema] = {}
    for p in schemas_dir.glob("*.json"):
        s = load_schema(p)
        schemas[s.workflow_id] = s
    return schemas


def load_dispositions(path: Path) -> dict[str, dict[str, Disposition]]:
    """Returns {workflow_id: {code: Disposition}}"""
    result: dict[str, dict[str, Disposition]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            wf = row["workflow_id"]
            d = Disposition(
                workflow_id=wf,
                code=row["code"],
                label=row["label"],
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
            result.setdefault(wf, {})[d.code] = d
    return result

