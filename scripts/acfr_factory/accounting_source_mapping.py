"""Explicit, loss-preserving column normalization for retained ledger exports."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

COLUMNS = ("entry_id", "leg_id", "account", "debit", "credit", "unit",
           "effective_date", "recorded_date", "evidence_id")
BANK_COLUMNS = ("transaction_id", "posted_date", "amount", "unit", "reference")
RECEIVABLES_COLUMNS = ("invoice_id", "customer_id", "issue_date", "due_date", "amount", "unit")


def normalize_export(content: str, mapping: dict) -> tuple[str, list[dict]]:
    return _normalize(content, mapping, COLUMNS, "accounting-column-map/1",
                      {"debit", "credit"}, {"effective_date", "recorded_date"}, {"debit", "credit"})


def normalize_bank_export(content: str, mapping: dict) -> tuple[str, list[dict]]:
    return _normalize(content, mapping, BANK_COLUMNS, "accounting-bank-column-map/1",
                      {"amount"}, {"posted_date"}, set())


def normalize_receivables_export(content: str, mapping: dict) -> tuple[str, list[dict]]:
    return _normalize(content, mapping, RECEIVABLES_COLUMNS, "accounting-receivables-column-map/1",
                      {"amount"}, {"issue_date", "due_date"}, set())


def _normalize(content, mapping, fields, schema, money_fields, date_fields, blank_zero_fields):
    if (set(mapping) != {"schema_version", "columns", "ignored_columns"}
            or mapping.get("schema_version") != schema):
        raise ValueError(f"mapping requires {schema}, columns and ignored_columns only")
    columns = mapping["columns"]
    ignored = mapping["ignored_columns"]
    if not isinstance(columns, dict) or set(columns) != set(fields):
        kind = "ledger" if fields == COLUMNS else "bank" if fields == BANK_COLUMNS else "receivables population"
        raise ValueError(f"map every required {kind} field explicitly")
    if (not isinstance(ignored, list) or any(not isinstance(k, str) or not k for k in ignored)
            or len(set(ignored)) != len(ignored)):
        raise ValueError("ignored_columns must contain unique column names")
    used = set()
    for field, spec in columns.items():
        if not isinstance(spec, dict):
            raise ValueError(f"mapping for {field} must be an object")
        if field == "unit" and spec == {"constant": "USD"}:
            continue
        allowed = {"column"}
        if field in money_fields:
            allowed.add("format")
            if field in blank_zero_fields:
                allowed.add("blank_as_zero")
            if spec.get("format", "decimal") not in {"decimal", "us_grouped"}:
                raise ValueError(f"unsupported money format for {field}")
            if "blank_as_zero" in spec and type(spec["blank_as_zero"]) is not bool:
                raise ValueError("blank_as_zero must be an explicit boolean")
        if field in date_fields:
            allowed.add("format")
            if spec.get("format", "iso") not in {"iso", "mdy_slash", "dmy_slash"}:
                raise ValueError(f"unsupported date format for {field}")
        if set(spec) - allowed or not isinstance(spec.get("column"), str) or not spec["column"]:
            raise ValueError(f"invalid column mapping for {field}; source columns are required")
        used.add(spec["column"])
    if used & set(ignored):
        raise ValueError("a mapped column cannot also be ignored")
    reader = csv.DictReader(io.StringIO(content.removeprefix("\ufeff")), strict=True)
    headers = reader.fieldnames or []
    if len(set(headers)) != len(headers) or set(headers) != used | set(ignored):
        raise ValueError("export headers must exactly match mapped and explicitly ignored columns")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    lineage = []
    previous_line = reader.line_num
    for record_number, row in enumerate(reader, 1):
        line_start = previous_line + 1
        previous_line = reader.line_num
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"export record {record_number}: column count mismatch")
        normalized = {}
        for field, spec in columns.items():
            value = spec.get("constant") if "constant" in spec else row[spec["column"]]
            try:
                if field in money_fields:
                    if value == "" and spec.get("blank_as_zero", False):
                        value = "0.00"
                    if spec.get("format", "decimal") == "us_grouped":
                        if not re.fullmatch(r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?", value):
                            raise ValueError("invalid explicitly declared US-grouped amount")
                        value = value.replace(",", "")
                if field in date_fields:
                    style = spec.get("format", "iso")
                    if style == "iso":
                        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                            raise ValueError("ISO dates require YYYY-MM-DD")
                        value = date.fromisoformat(value).isoformat()
                    else:
                        if not re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", value):
                            raise ValueError("slash dates require an explicit four-digit year")
                        value = datetime.strptime(value, "%m/%d/%Y" if style == "mdy_slash" else "%d/%m/%Y").date().isoformat()
            except ValueError as exc:
                raise ValueError(f"export record {record_number}, {field}: {exc}") from exc
            normalized[field] = value
        writer.writerow(normalized)
        lineage.append({"source_record": record_number, "source_line_start": line_start,
                        "source_line_end": reader.line_num})
    return output.getvalue(), lineage
