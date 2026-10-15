"""Read-only USD evidence evaluation over a retained, versioned file closure."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from .link_contract import fingerprint
from .accounting_source_mapping import normalize_export

SCHEMA = "accounting-evidence/1"
LEDGER_COLUMNS = {"entry_id", "leg_id", "account", "debit", "credit", "unit",
                  "effective_date", "recorded_date", "evidence_id"}
NONCLAIMS = ["source-authenticity", "completeness-outside-retained-extract",
             "accounting-treatment-opinion", "live-native-readback", "client-readiness"]


def money(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", value):
        raise ValueError("amount must be an exact decimal string with at most two places")
    whole, _, fraction = value.lstrip("-").partition(".")
    return (-1 if value.startswith("-") else 1) * (int(whole) * 100 + int(fraction.ljust(2, "0") or "0"))


def dollars(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_object(text: str) -> dict:
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON constant: {value}")
    result = json.loads(text.removeprefix("\ufeff"), object_pairs_hook=unique_object,
                        parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("JSON input must be an object")
    return result


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def validate_manifest(manifest: dict) -> None:
    allowed = {"schema_version", "scope", "unit", "chart_evidence_id", "period_start", "period_end",
               "known_at", "ledger", "sources", "claims", "description"}
    if set(manifest) - allowed:
        raise ValueError("unknown manifest field(s): " + ", ".join(sorted(set(manifest) - allowed)))
    if manifest.get("schema_version") != SCHEMA or manifest.get("unit") != "USD":
        raise ValueError("supported schema/unit: accounting-evidence/1, USD")
    text(manifest.get("scope"), "scope")
    text(manifest.get("chart_evidence_id"), "chart_evidence_id")
    start = date.fromisoformat(text(manifest.get("period_start"), "period_start"))
    end = date.fromisoformat(text(manifest.get("period_end"), "period_end"))
    date.fromisoformat(text(manifest.get("known_at"), "known_at"))
    if end < start:
        raise ValueError("period_end precedes period_start")
    ledger = manifest.get("ledger")
    if not isinstance(ledger, dict) or ledger.get("completeness") != "provided_extract_only":
        raise ValueError("ledger must declare completeness: provided_extract_only")
    if set(ledger) - {"path", "sha256", "completeness", "mapping"}:
        raise ValueError("unknown ledger field")
    text(ledger.get("path"), "ledger.path")
    if "mapping" in ledger:
        mapping = ledger["mapping"]
        if not isinstance(mapping, dict) or set(mapping) - {"path", "sha256"}:
            raise ValueError("ledger.mapping must be a path/sha256 reference")
        text(mapping.get("path"), "ledger.mapping.path")
    if not isinstance(manifest.get("sources"), list) or not isinstance(manifest.get("claims"), list):
        raise ValueError("sources and claims must be lists")
    if not manifest["claims"]:
        raise ValueError("at least one claim is required")
    source_ids = {"ledger", "ledger-mapping"}
    for ref in manifest["sources"]:
        if not isinstance(ref, dict):
            raise ValueError("source reference must be an object")
        ident = text(ref.get("id"), "source.id")
        text(ref.get("path"), "source.path")
        if ident in source_ids:
            raise ValueError("duplicate source id")
        source_ids.add(ident)
    seen = set()
    for claim in manifest["claims"]:
        if not isinstance(claim, dict):
            raise ValueError("claim must be an object")
        allowed_claim = {"id", "label", "accounts", "normal_side", "opening_amount",
                         "opening_evidence_id", "bank_evidence_id", "reconciliation_evidence_id",
                         "bank_transactions_id", "bank_transactions_mapping_id",
                         "bank_opening_evidence_id", "bank_links_evidence_id", "receivables_evidence_id"}
        if set(claim) - allowed_claim:
            raise ValueError("unknown claim field(s): " + ", ".join(sorted(set(claim) - allowed_claim)))
        ident = text(claim.get("id"), "claim.id")
        if "label" in claim:
            text(claim["label"], "claim.label")
        if ident in seen:
            raise ValueError("duplicate claim id")
        seen.add(ident)
        accounts = claim.get("accounts")
        if not isinstance(accounts, list) or not accounts or any(not isinstance(a, str) or not a for a in accounts):
            raise ValueError("claim accounts must be a nonempty list of account codes")
        if len(set(accounts)) != len(accounts):
            raise ValueError("duplicate account in claim mapping")
        if claim.get("normal_side") not in {"debit", "credit"}:
            raise ValueError("claim normal_side must be debit or credit")
        text(claim.get("opening_evidence_id"), "opening_evidence_id")
        money(claim.get("opening_amount"))
        if "receivables_evidence_id" in claim:
            text(claim["receivables_evidence_id"], "receivables_evidence_id")
            if claim["normal_side"] != "debit":
                raise ValueError("invoice receivables schedules require a debit-normal claim")
        if "bank_evidence_id" in claim:
            text(claim["bank_evidence_id"], "bank_evidence_id")
        if "reconciliation_evidence_id" in claim:
            text(claim["reconciliation_evidence_id"], "reconciliation_evidence_id")
            if "bank_evidence_id" not in claim or claim["normal_side"] != "debit":
                raise ValueError("bank timing schedules require a debit-normal bank comparison")
        bank_fields = {"bank_transactions_id", "bank_transactions_mapping_id",
                       "bank_opening_evidence_id", "bank_links_evidence_id"}
        if bank_fields & claim.keys():
            for key in bank_fields - {"bank_transactions_mapping_id"}:
                text(claim.get(key), key)
            if "bank_transactions_mapping_id" in claim:
                text(claim["bank_transactions_mapping_id"], "bank_transactions_mapping_id")
            if "bank_evidence_id" not in claim or claim["normal_side"] != "debit":
                raise ValueError("bank rows require a debit-normal bank comparison")


def source_refs(manifest: dict) -> list[tuple[str, dict]]:
    refs = [("ledger", manifest["ledger"])]
    if "mapping" in manifest["ledger"]:
        refs.append(("ledger-mapping", manifest["ledger"]["mapping"]))
    return refs + [(ref["id"], ref) for ref in manifest["sources"]]


def capture(manifest_path: Path) -> dict:
    manifest = json_object(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    refs = source_refs(manifest)
    files = []
    seen = set()
    for ident, ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("source reference must be an object")
        if ident in seen:
            raise ValueError("duplicate source id")
        seen.add(ident)
        name = text(ref.get("path"), "source.path")
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = manifest_path.parent / path
        expected = ref.get("sha256")
        record = {"id": ident, "path": name, "expected_sha256": expected, "content": None,
                  "actual_sha256": None, "problem": None}
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            record["problem"] = "missing_or_invalid_source_revision"
        try:
            raw = path.read_bytes()
            record["actual_sha256"] = hashlib.sha256(raw).hexdigest()
            record["content"] = raw.decode("utf-8")
            if record["problem"] is None and record["actual_sha256"] != expected:
                record["problem"] = "source_revision_changed"
        except (OSError, UnicodeError):
            record["problem"] = "missing_or_unreadable_source"
        files.append(record)
    return {"schema_version": SCHEMA, "manifest": manifest, "files": files}


def issue(code: str, subject: str, detail: str) -> dict:
    return {"code": code, "subject": subject, "detail": detail}


def evaluate_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("retained snapshot must be an object")
    if snapshot.get("schema_version") != SCHEMA:
        raise ValueError("unsupported snapshot schema")
    manifest = snapshot["manifest"]
    if not isinstance(manifest, dict) or not isinstance(snapshot.get("files"), list):
        raise ValueError("retained snapshot requires a manifest object and files list")
    validate_manifest(manifest)
    files = {}
    for record in snapshot["files"]:
        if not isinstance(record, dict):
            raise ValueError("retained source record must be an object")
        ident = text(record.get("id"), "retained source.id")
        if ident in files:
            raise ValueError("duplicate retained source id")
        files[ident] = record
        content = record.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("retained content must be a string or null")
    declared = {ident for ident, ref in source_refs(manifest)}
    if set(files) != declared:
        raise ValueError("retained sources do not match declared source ids")
    for ident, ref in source_refs(manifest):
        record = files[ident]
        if record["path"] != ref["path"] or record["expected_sha256"] != ref.get("sha256"):
            raise ValueError("retained source metadata differs from its manifest")
        content = record["content"]
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None
        if content is not None and actual != record["actual_sha256"]:
            raise ValueError("retained source bytes disagree with their hash")
        expected = ref.get("sha256")
        problem = None
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            problem = "missing_or_invalid_source_revision"
        if content is None:
            problem = "missing_or_unreadable_source"
        elif problem is None and actual != expected:
            problem = "source_revision_changed"
        if record["problem"] != problem:
            raise ValueError("retained source problem state disagrees with its bytes")
    ledger = files["ledger"]
    global_issues = []
    if ledger["problem"]:
        global_issues.append(issue(ledger["problem"], "ledger", "Resolve the ledger revision before accepting dependent numbers."))
    ledger_content = ledger["content"]
    lineage = None
    normalization = None
    if "mapping" in manifest["ledger"]:
        mapping_file = files["ledger-mapping"]
        if mapping_file["problem"]:
            global_issues.append(issue(mapping_file["problem"], "ledger-mapping", "Review and retain the controlling export mapping revision."))
        try:
            if mapping_file["content"] is None or ledger_content is None:
                raise ValueError("export and mapping bytes are both required")
            mapping = json_object(mapping_file["content"])
            ledger_content, lineage = normalize_export(ledger_content, mapping)
            normalization = {"source_records": len(lineage),
                             "normalized_ledger_sha256": hashlib.sha256(ledger_content.encode("utf-8")).hexdigest(),
                             "mapping_revision": mapping_file["actual_sha256"],
                             "recorded_history": "supplied_source_field", "rules": mapping}
        except (ValueError, TypeError, csv.Error) as exc:
            global_issues.append(issue("invalid_export_normalization", "ledger", str(exc)))
            ledger_content = None
    groups = {}
    if ledger_content is not None:
        reader = csv.DictReader(io.StringIO(ledger_content.removeprefix("\ufeff")), strict=True)
        try:
            headers = reader.fieldnames or []
            if len(set(headers)) != len(headers) or not LEDGER_COLUMNS.issubset(headers):
                global_issues.append(issue("invalid_ledger_columns", "ledger", "Use the documented, unique CSV headers."))
            else:
                for ordinal, row in enumerate(reader, 2):
                    try:
                        if None in row or any(value is None for value in row.values()):
                            raise ValueError("column count mismatch")
                        for key in ("entry_id", "leg_id", "account", "evidence_id"):
                            text(row[key], key)
                        effective = date.fromisoformat(row["effective_date"])
                        recorded = date.fromisoformat(row["recorded_date"])
                        debit, credit = money(row["debit"]), money(row["credit"])
                        if debit < 0 or credit < 0 or (debit > 0) == (credit > 0) or row["unit"] != "USD":
                            raise ValueError("one positive USD debit or credit is required per leg")
                        leg = {**row, "row": ordinal, "debit_cents": debit, "credit_cents": credit,
                               "effective": effective, "recorded": recorded}
                        if lineage is not None:
                            leg["source_lineage"] = lineage[ordinal - 2]
                        groups.setdefault(row["entry_id"], []).append(leg)
                    except (ValueError, TypeError) as exc:
                        global_issues.append(issue("invalid_ledger_row", str(ordinal), str(exc)))
        except csv.Error as exc:
            global_issues.append(issue("invalid_ledger_csv", "ledger", f"CSV quotation/structure error near physical line {reader.reader.line_num}: {exc}"))
    else:
        global_issues.append(issue("ledger_unavailable", "ledger", "No retained ledger bytes are available."))

    documents = {}
    document_problems = {}
    bank_csv_ids = {c["bank_transactions_id"] for c in manifest["claims"] if "bank_transactions_id" in c}
    for ident, record in files.items():
        if ident in {"ledger", "ledger-mapping"} or ident in bank_csv_ids:
            continue
        if record["problem"]:
            document_problems[ident] = record["problem"]
            continue
        try:
            documents[ident] = json_object(record["content"])
        except (ValueError, TypeError):
            document_problems[ident] = "invalid_evidence_json"

    chart_id = manifest["chart_evidence_id"]
    chart = documents.get(chart_id, {})
    chart_accounts = chart.get("accounts")
    if chart_id not in files or chart_id in document_problems:
        global_issues.append(issue(document_problems.get(chart_id, "missing_chart_evidence"), chart_id,
                                   "Retain the controlling account inventory before checking mappings."))
        known_accounts = set()
    elif (chart.get("schema_version") != "accounting-chart/1" or chart.get("scope") != manifest["scope"]
          or not isinstance(chart_accounts, list) or not chart_accounts
          or any(not isinstance(a, str) or not a for a in chart_accounts)
          or len(set(chart_accounts)) != len(chart_accounts)):
        global_issues.append(issue("invalid_chart_evidence", chart_id, "Account inventory must match the scope and contain unique account codes."))
        known_accounts = set()
    else:
        known_accounts = set(chart_accounts)

    def source_check(ident, kind, subject, amount=None, effective=None):
        if ident not in files:
            return [issue("missing_evidence_reference", ident, "Declare and retain the required evidence file.")]
        if ident in document_problems:
            return [issue(document_problems[ident], ident, "Restore or re-pin the controlling evidence after review.")]
        doc = documents.get(ident, {})
        expected_fields = {"schema_version": "accounting-source/1", "unit": "USD", "kind": kind, "subject": subject}
        if effective is not None:
            expected_fields["effective_date"] = effective
        differences = [f"{key}: expected {expected!r}, retained {doc.get(key)!r}"
                       for key, expected in expected_fields.items() if doc.get(key) != expected]
        try:
            actual = money(doc.get("amount"))
            if amount is not None and actual != amount:
                differences.append(f"amount: expected {dollars(amount)} USD, retained {dollars(actual)} USD")
        except ValueError:
            differences.append("amount is not a supported exact decimal string")
        return [] if not differences else [issue("evidence_conflict", ident, "; ".join(differences))]

    period_start = date.fromisoformat(manifest["period_start"])
    period_end = date.fromisoformat(manifest["period_end"])
    known = date.fromisoformat(manifest["known_at"])
    selected = {}
    entry_issues = {}
    for entry_id, legs in groups.items():
        coordinates = {(x["effective"], x["recorded"], x["evidence_id"]) for x in legs}
        if len(coordinates) != 1:
            global_issues.append(issue("entry_coordinate_conflict", entry_id, "All legs of an entry must share its dates and evidence."))
            continue
        effective, recorded, evidence_id = next(iter(coordinates))
        if not (period_start <= effective <= period_end and recorded <= known):
            continue
        selected[entry_id] = legs
        problems = []
        for account in sorted({x["account"] for x in legs} - known_accounts):
            problems.append(issue("unknown_ledger_account", entry_id, f"Account {account!r} is absent from the retained account inventory."))
        if len({x["leg_id"] for x in legs}) != len(legs):
            problems.append(issue("duplicate_leg", entry_id, "Resolve duplicate imported legs; none is silently discarded."))
        if sum(x["debit_cents"] - x["credit_cents"] for x in legs) != 0:
            problems.append(issue("unbalanced_entry", entry_id, "Retained entry debits and credits do not balance exactly."))
        gross = sum(x["debit_cents"] for x in legs)
        problems.extend(source_check(evidence_id, "transaction", entry_id, gross, effective.isoformat()))
        entry_issues[entry_id] = problems

    claims = []
    exceptions = []
    for claim in manifest["claims"]:
        problems = list(global_issues)
        ident = claim["id"]
        for account in sorted(set(claim["accounts"]) - known_accounts):
            problems.append(issue("unknown_mapping_account", ident, f"Mapped account {account!r} is absent from the retained account inventory."))
        opening = money(claim["opening_amount"])
        problems.extend(source_check(claim["opening_evidence_id"], "opening_balance", ident,
                                     opening, manifest["period_start"]))
        baseline = documents.get(claim["opening_evidence_id"])
        if baseline and (baseline.get("accounts") != claim["accounts"] or baseline.get("normal_side") != claim["normal_side"]):
            problems.append(issue("opening_mapping_conflict", claim["opening_evidence_id"], "Retained opening evidence does not bind this account mapping and normal side."))
        sign = 1 if claim["normal_side"] == "debit" else -1
        trace = []
        for entry_id in sorted(selected):
            legs = selected[entry_id]
            contributions = [x for x in legs if x["account"] in claim["accounts"]]
            if not contributions:
                continue
            problems.extend(entry_issues[entry_id])
            for leg in contributions:
                trace.append({"entry_id": entry_id, "leg_id": leg["leg_id"], "ledger_row": leg["row"],
                              "account": leg["account"], "signed_cents": sign * (leg["debit_cents"] - leg["credit_cents"]),
                              "effective_date": leg["effective_date"], "recorded_date": leg["recorded_date"],
                              "evidence_id": leg["evidence_id"], "evidence_revision": files.get(leg["evidence_id"], {}).get("actual_sha256")})
                if "source_lineage" in leg:
                    trace[-1].update(leg["source_lineage"], ledger_revision=ledger["actual_sha256"],
                                     mapping_revision=files["ledger-mapping"]["actual_sha256"])
        activity = sum(x["signed_cents"] for x in trace)
        ending = opening + activity
        residual = None
        bank_adjustment = 0
        reconciling_items = []
        bank_id = claim.get("bank_evidence_id")
        bank_ending = None
        if bank_id:
            bank_problems = source_check(bank_id, "bank_balance", ident, effective=manifest["period_end"])
            problems.extend(bank_problems)
            if not bank_problems:
                bank_ending = money(documents[bank_id]["amount"])
                schedule_id = claim.get("reconciliation_evidence_id")
                if schedule_id:
                    schedule = documents.get(schedule_id, {})
                    items = schedule.get("items")
                    if schedule_id not in files or schedule_id in document_problems:
                        problems.append(issue(document_problems.get(schedule_id, "missing_reconciliation_evidence"), schedule_id,
                                              "Retain the timing schedule and each item's evidence."))
                    elif (schedule.get("schema_version") != "accounting-reconciliation/1"
                          or schedule.get("subject") != ident or schedule.get("unit") != "USD"
                          or schedule.get("effective_date") != manifest["period_end"] or not isinstance(items, list)):
                        problems.append(issue("invalid_reconciliation_schedule", schedule_id, "Timing schedule subject, unit, cutoff or item list is invalid."))
                    else:
                        seen_ids, seen_entries = set(), set()
                        for item in items:
                            try:
                                item_id = text(item.get("id"), "timing item.id")
                                entry_id = text(item.get("entry_id"), "timing item.entry_id")
                                evidence_id = text(item.get("evidence_id"), "timing item.evidence_id")
                                amount = money(item.get("amount"))
                                if amount <= 0 or item.get("kind") not in {"outstanding_payment", "deposit_in_transit"}:
                                    raise ValueError("supported timing kinds require a positive exact amount")
                                if item_id in seen_ids or entry_id in seen_entries:
                                    raise ValueError("a timing item or ledger entry is counted more than once")
                                seen_ids.add(item_id); seen_entries.add(entry_id)
                                expected_effect = amount if item["kind"] == "deposit_in_transit" else -amount
                                contribution = sum(x["signed_cents"] for x in trace if x["entry_id"] == entry_id)
                                if contribution != expected_effect or entry_id not in selected:
                                    raise ValueError("timing amount does not match a visible retained cash entry")
                                entry_problems = entry_issues[entry_id]
                                item_problems = source_check(evidence_id, "timing_item", item_id, amount, manifest["period_end"])
                                supporting = documents.get(evidence_id, {})
                                if supporting.get("timing_kind") != item["kind"] or supporting.get("entry_id") != entry_id:
                                    item_problems.append(issue("timing_evidence_conflict", evidence_id, "Timing evidence must bind the supplied kind and exact ledger entry."))
                                problems.extend(item_problems)
                                if entry_problems or item_problems:
                                    continue
                                bank_adjustment += expected_effect
                                reconciling_items.append({"id": item_id, "kind": item["kind"], "entry_id": entry_id,
                                                          "evidence_id": evidence_id, "bank_adjustment_cents": expected_effect})
                            except (ValueError, AttributeError) as exc:
                                problems.append(issue("invalid_timing_item", schedule_id, str(exc)))
                residual = money(documents[bank_id]["amount"]) + bank_adjustment - ending
                if residual:
                    problems.append(issue("unresolved_bank_difference", bank_id, f"Adjusted bank minus ledger is {dollars(residual)} USD; classify the difference."))
        bank_rows = None
        if "bank_transactions_id" in claim:
            from .accounting_bank import reconcile_bank
            opening_id = claim["bank_opening_evidence_id"]
            opening_problems = source_check(opening_id, "bank_opening_balance", ident,
                                            effective=manifest["period_start"])
            problems.extend(opening_problems)
            bank_opening = None if opening_problems else money(documents[opening_id]["amount"])
            bank_rows, row_problems = reconcile_bank(claim, manifest, files, trace, entry_issues,
                                                    reconciling_items, bank_opening, bank_ending,
                                                    ledger_basis_ok=not global_issues)
            problems.extend(row_problems)
        receivables = None
        if "receivables_evidence_id" in claim:
            from .accounting_receivables import reconcile_receivables
            receivables, invoice_problems = reconcile_receivables(
                claim, manifest, files, trace, entry_issues, opening, ending,
                ledger_basis_ok=not global_issues)
            problems.extend(invoice_problems)
        deduplicated = {fingerprint(p): p for p in problems}
        problems = list(deduplicated.values())
        result = {"id": ident, "label": claim.get("label", ident), "status": "unresolved" if problems else "supported",
                  "accounts": claim["accounts"], "normal_side": claim["normal_side"], "unit": "USD",
                  "opening_cents": opening, "activity_cents": activity, "ending_cents": ending,
                  "bank_difference_cents": residual, "trace": trace, "issues": problems,
                  "bank_adjustment_cents": bank_adjustment, "reconciling_items": reconciling_items,
                  "numerical_basis": "provisional" if problems else "retained-inputs-checked"}
        if "bank_transactions_id" in claim:
            result["bank_reconciliation"] = bank_rows
        if receivables is not None:
            result["receivables_reconciliation"] = receivables
        claims.append(result)
        for problem in problems:
            exceptions.append({"id": "evidence-" + fingerprint({"claim": ident, **problem})[:20],
                               "severity": "blocking", "status": "open", "owner": "preparer",
                               "rationale": problem["detail"], "affected_gates": ["source_intake_ready"],
                               "claim_id": ident, "code": problem["code"], "subject": problem["subject"]})
    report = {"schema_version": SCHEMA, "scope": manifest["scope"], "unit": "USD",
            "period_start": manifest["period_start"], "period_end": manifest["period_end"],
            "known_at": manifest["known_at"], "input_fingerprint": fingerprint(snapshot),
            "status": "pass" if all(c["status"] == "supported" for c in claims) else "fail",
            "claims": claims, "exception_register": {"schema_version": "2026-06-14", "exceptions": exceptions},
            "source_inventory": [{k: f[k] for k in ("id", "path", "expected_sha256", "actual_sha256", "problem")}
                                 for f in snapshot["files"]],
            "not_claimed": NONCLAIMS}
    if "mapping" in manifest["ledger"]:
        report["normalization"] = normalization
    return report


def evaluate_manifest(path: Path) -> dict:
    return evaluate_snapshot(capture(path))


def pack_for(path: Path) -> dict:
    snapshot = capture(path)
    return {"schema_version": SCHEMA, "snapshot": snapshot, "report": evaluate_snapshot(snapshot)}


def recheck_pack(pack: dict, expected: str) -> dict:
    """Check the sealed input closure and derive current facts without trusting saved decisions."""
    if not isinstance(pack, dict):
        raise ValueError("retained pack must be an object")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or fingerprint(pack) != expected:
        raise ValueError("pack fingerprint does not match independently retained digest")
    if pack.get("schema_version") != SCHEMA:
        raise ValueError("unsupported pack schema")
    if not isinstance(pack.get("report"), dict):
        raise ValueError("retained report must be an object")
    report = evaluate_snapshot(pack["snapshot"])
    return {"schema_version": SCHEMA, "snapshot": pack["snapshot"], "report": report}


def evaluator_descriptor() -> dict:
    """Identify the local evaluation sources/runtime; this is not an execution attestation."""
    import platform
    names = ("accounting_evidence.py", "accounting_source_mapping.py", "accounting_bank.py",
             "accounting_receivables.py", "link_contract.py")
    return {"python": platform.python_version(),
            "files": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}}


def verify_pack(pack: dict, expected: str) -> dict:
    report = recheck_pack(pack, expected)["report"]
    if report != pack["report"]:
        raise ValueError("retained report differs from recomputation")
    return report


def markdown_text(value) -> str:
    value = html.escape(str(value), quote=True).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*{}\[\]()#+!_|])", r"\\\1", value)


def review_markdown(report: dict) -> str:
    safe = markdown_text
    lines = ["# Accounting evidence review", "", f"Scope: {safe(report['scope'])}",
             f"Period: {report['period_start']} through {report['period_end']}; known at {report['known_at']}.",
             "", "Amounts marked unresolved are provisional. Support means the stated retained-input checks passed.",
             "", "| Claim | Opening USD | Activity USD | Ending USD | Status |", "|---|---:|---:|---:|---|"]
    for claim in report["claims"]:
        lines.append(f"| {safe(claim['label'])} | {dollars(claim['opening_cents'])} | {dollars(claim['activity_cents'])} | {dollars(claim['ending_cents'])} | {claim['status']} |")
    for claim in report["claims"]:
        lines.extend(["", f"## {safe(claim['label'])}", ""])
        for leg in claim["trace"]:
            lines.append(f"- {safe(leg['entry_id'])}/{safe(leg['leg_id'])}: {dollars(leg['signed_cents'])} USD; GL row {leg['ledger_row']}; evidence {safe(leg['evidence_id'])}; effective {leg['effective_date']}, recorded {leg['recorded_date']}.")
            if "source_record" in leg:
                lines.append(f"  Original export record {leg['source_record']}, physical lines {leg['source_line_start']}–{leg['source_line_end']}; retained export and mapping revisions are bound in the pack.")
        for problem in claim["issues"]:
            lines.append(f"- **Unresolved — {safe(problem['code'])}:** {safe(problem['detail'])} ({safe(problem['subject'])}).")
        for item in claim["reconciling_items"]:
            lines.append(f"- Timing item {safe(item['id'])}: {safe(item['kind'])}, bank adjustment {dollars(item['bank_adjustment_cents'])} USD; ledger entry {safe(item['entry_id'])}; evidence {safe(item['evidence_id'])}.")
        if "bank_reconciliation" in claim:
            bank = claim["bank_reconciliation"]
            amount = lambda key: "unavailable" if bank[key] is None else dollars(bank[key]) + " USD"
            count = lambda key: "unavailable" if bank[key] is None else str(bank[key])
            lines += ["", "### Bank transaction comparison", "",
                      f"Bank comparison: {safe(bank['status'])}; retained records: {count('retained_record_count')}; selected records: {count('selected_record_count')}.",
                      f"Opening {amount('bank_opening_cents')} + bank activity {amount('bank_activity_cents')} = {amount('calculated_bank_ending_cents')}; reported ending {amount('reported_bank_ending_cents')}.",
                      f"Reported ending minus calculated ending: {amount('bank_rollforward_difference_cents')}.",
                      "Bank posting dates select the statement period; bank recording history is not supplied. Amount-only candidates remain suggestions.", ""]
            for match in bank["matches"]:
                row = match["bank_row"]
                lines.append(f"- Bound bank row {safe(row['transaction_id'])} to ledger entry {safe(match['entry_id'])}: {dollars(row['amount_cents'])} USD; posted {row['posted_date']}; original record {row['source_record']}, lines {row['source_line_start']}–{row['source_line_end']}; reference {safe(row['reference'])}.")
            for group in bank.get("settlement_groups", []):
                lines.append(f"- Bound settlement {safe(group['id'])}: bank total {dollars(group['bank_total_cents'])} USD = ledger total {dollars(group['ledger_total_cents'])} USD; retained evidence {safe(group['evidence_id'])} ({group['evidence_sha256']}); rationale {safe(group['rationale'])}.")
                for row in group["bank_rows"]:
                    lines.append(f"  - Bank member {safe(row['transaction_id'])}: {dollars(row['amount_cents'])} USD; posted {row['posted_date']}; original record {row['source_record']}, lines {row['source_line_start']}–{row['source_line_end']}; reference {safe(row['reference'])}.")
                for entry in group["ledger_entries"]:
                    lines.append(f"  - Full ledger member {safe(entry['entry_id'])}: {dollars(entry['amount_cents'])} USD; effective {entry['effective_date']}, recorded {entry['recorded_date']}; evidence {safe(entry['evidence_id'])}.")
            for row in bank["unmatched_bank_rows"]:
                candidates = ", ".join(row["candidate_ledger_entries"]) or "none"
                lines.append(f"- **Unmatched bank row {safe(row['transaction_id'])}:** {dollars(row['amount_cents'])} USD; posted {row['posted_date']}; original record {row['source_record']}, lines {row['source_line_start']}–{row['source_line_end']}; candidate entries {safe(candidates)}; reference {safe(row['reference'])}.")
            for entry in bank["unmatched_ledger_entries"]:
                lines.append(f"- **Unmatched ledger entry {safe(entry['entry_id'])}:** {dollars(entry['amount_cents'])} USD; effective {entry['effective_date']}, recorded {entry['recorded_date']}.")
            for row in bank["excluded_bank_rows"]:
                lines.append(f"- Bank row {safe(row['transaction_id'])} is outside the statement period: {dollars(row['amount_cents'])} USD; posted {row['posted_date']}; original record {row['source_record']}.")
        if "receivables_reconciliation" in claim:
            ar = claim["receivables_reconciliation"]
            amount = lambda key: "unavailable" if ar[key] is None else dollars(ar[key]) + " USD"
            retained_count = "unavailable" if ar["retained_movement_count"] is None else str(ar["retained_movement_count"])
            lines += ["", "### Invoice receivables support", "",
                      f"Population checks: {safe(ar['status'])}; opening {amount('opening_population_cents')}; allocated activity {amount('allocated_activity_cents')}; ending {amount('ending_population_cents')}.",
                      f"Opening population minus ledger: {amount('opening_minus_ledger_cents')}; ending population minus ledger: {amount('ending_minus_ledger_cents')}.",
                      f"Parsed {ar['parsed_movement_count']} of {retained_count} retained movement definitions. Population recording history and intraday ordering are not supplied.", ""]
            value = lambda cents: "unavailable" if cents is None else dollars(cents) + " USD"
            for invoice in ar["invoices"]:
                lines.append(f"- Invoice {safe(invoice['invoice_id'])}, customer {safe(invoice['customer_id'])}: opening {value(invoice['opening_cents'])} + movements {value(invoice['movement_cents'])} = calculated {value(invoice['calculated_ending_cents'])}; reported {value(invoice['reported_ending_cents'])}; {safe(invoice['status'])}; ending presence {safe(invoice['ending_presence'])}; overdue days {invoice['overdue_days']}.")
                for label in ("opening", "ending"):
                    row = invoice[label + "_record"]
                    if row: lines.append(f"  - {label.capitalize()} original record {row['source_record']}, lines {row['source_line_start']}–{row['source_line_end']}.")
            for movement in ar["movements"]:
                lines.append(f"- Movement {safe(movement['id'])}: {safe(movement['kind'])}, invoice {safe(movement['invoice_id'])}, {value(movement['amount_cents'])}; full ledger entry {safe(movement['entry_id'])}; effective {movement['effective_date']}, recorded {movement['recorded_date']}; evidence {safe(movement['evidence_id'])}; {safe(movement['status'])}.")
            for row in ar["invalid_movements"]:
                lines.append(f"- **Invalid movement record {row['source_record']} ({safe(row['subject'])}):** {safe(row['detail'])}.")
            for entry in ar["entry_coverage"]:
                lines.append(f"- Full entry {safe(entry['entry_id'])}: {value(entry['amount_cents'])}; allocations {value(entry['allocated_cents'])}; difference {value(entry['difference_cents'])}; {safe(entry['status'])}.")
            if ar["aging_cents"] is not None:
                lines.append("- Reported ending population aging USD: " + safe(", ".join(f"{key} {dollars(ar['aging_cents'][key])}" for key in ("current", "1_30", "31_60", "61_90", "91_plus"))) + ". This is date arithmetic on supplied terms, not a collectibility or allowance opinion.")
    if report.get("normalization"):
        normalized = report["normalization"]
        lines += ["", "## Export normalization", "",
                  f"All {normalized['source_records']} export records were retained and mapped; recorded dates came from the supplied source field.",
                  f"Normalized ledger SHA256: {normalized['normalized_ledger_sha256']}.",
                  "", "Declared transformations:", ""]
        for field, rule in normalized["rules"]["columns"].items():
            lines.append(f"- {safe(field)}: {safe(json.dumps(rule, sort_keys=True))}.")
        lines.append(f"- Explicitly ignored columns (original bytes retained): {safe(', '.join(normalized['rules']['ignored_columns']) or 'none')}.")
    lines += ["", "## Retained sources", "", "| Source | File | Revision check |", "|---|---|---|"]
    for source in report["source_inventory"]:
        state = source["problem"] or "matched"
        lines.append(f"| {safe(source['id'])} | {safe(source['path'])} | {safe(state)} |")
    lines += ["", "The opening amount is a retained baseline. This review does not prove source authenticity, full ledger completeness, accounting treatment, native readback or client readiness.", ""]
    return "\n".join(lines)
