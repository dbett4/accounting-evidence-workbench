"""Read-only bank-row reconciliation; proposed matches never grant acceptance."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date

from .accounting_evidence import dollars, issue, json_object, money, text
from .accounting_source_mapping import BANK_COLUMNS, normalize_bank_export
from .link_contract import fingerprint


def bank_link_basis(claim, manifest, files):
    refs = ["ledger", claim["bank_transactions_id"]]
    if "mapping" in manifest["ledger"]:
        refs.append("ledger-mapping")
    if "bank_transactions_mapping_id" in claim:
        refs.append(claim["bank_transactions_mapping_id"])
    basis = {"sources": {ident: files.get(ident, {}).get("actual_sha256") for ident in refs},
             "scope": manifest["scope"], "unit": manifest["unit"],
             "period_start": manifest["period_start"], "period_end": manifest["period_end"],
             "known_at": manifest["known_at"], "claim_id": claim["id"],
             "accounts": claim["accounts"], "normal_side": claim["normal_side"]}
    return fingerprint(basis), all(ident in files and not files[ident]["problem"] for ident in refs)


def _source(files, ident):
    record = files.get(ident)
    if record is None:
        raise ValueError(f"undeclared source: {ident}")
    if record["problem"] or record["content"] is None:
        raise ValueError(f"{ident}: {record['problem'] or 'source unavailable'}")
    return record


def _rows(record, mapping):
    normalized, lineage = normalize_bank_export(record["content"], mapping)
    rows = []
    ids = set()
    for row, origin in zip(csv.DictReader(io.StringIO(normalized)), lineage, strict=True):
        ident = text(row["transaction_id"], "bank transaction_id")
        amount = money(row["amount"])
        if ident in ids or row["unit"] != "USD" or amount == 0:
            raise ValueError(f"bank record {origin['source_record']}: duplicate ID, unsupported currency or zero amount")
        ids.add(ident)
        rows.append({"transaction_id": ident, "posted_date": row["posted_date"],
                     "amount_cents": amount, "reference": row["reference"], **origin,
                     "source_revision": record["actual_sha256"]})
    return rows, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def reconcile_bank(claim, manifest, files, trace, entry_issues, timing, opening, ending,
                   *, ledger_basis_ok):
    problems = []
    ident = claim["bank_transactions_id"]
    raw = files.get(ident)
    rows, normalized_hash = None, None
    mapping_id = claim.get("bank_transactions_mapping_id")
    mapping = {"schema_version": "accounting-bank-column-map/1",
               "columns": {field: {"column": field} for field in BANK_COLUMNS}, "ignored_columns": []}
    try:
        raw = _source(files, ident)
        if mapping_id:
            mapping = json_object(_source(files, mapping_id)["content"])
        rows, normalized_hash = _rows(raw, mapping)
    except (ValueError, TypeError, csv.Error) as exc:
        problems.append(issue("invalid_bank_transactions", ident, str(exc)))
    basis, basis_ready = bank_link_basis(claim, manifest, files)
    basis_ready = basis_ready and rows is not None and ledger_basis_ok
    links, groups = [], None
    links_id = claim["bank_links_evidence_id"]
    try:
        doc = json_object(_source(files, links_id)["content"])
        fields = {"schema_version", "subject", "basis_sha256", "matches"}
        if doc.get("schema_version") == "accounting-bank-links/2":
            fields.add("groups")
        if (set(doc) != fields
                or doc.get("schema_version") not in {"accounting-bank-links/1", "accounting-bank-links/2"}
                or doc.get("subject") != claim["id"] or not isinstance(doc.get("matches"), list)
                or ("groups" in doc and not isinstance(doc["groups"], list))):
            raise ValueError("bank links require this claim subject and an explicit matches list")
        if doc.get("basis_sha256") != basis:
            raise ValueError("bank links belong to different source revisions, mappings or reporting coordinates; prepare a reviewed new link version")
        links = doc["matches"]
        groups = doc.get("groups")
    except (ValueError, TypeError) as exc:
        problems.append(issue("invalid_bank_links", links_id, str(exc)))

    start, end = date.fromisoformat(manifest["period_start"]), date.fromisoformat(manifest["period_end"])
    selected = [r for r in rows or [] if start <= date.fromisoformat(r["posted_date"]) <= end]
    excluded = [dict(r, reason="outside_statement_period") for r in rows or []
                if not start <= date.fromisoformat(r["posted_date"]) <= end]
    bank = {row["transaction_id"]: row for row in selected}
    entries = {}
    for leg in trace:
        entry = entries.setdefault(leg["entry_id"], {"entry_id": leg["entry_id"], "amount_cents": 0,
                                  "effective_date": leg["effective_date"], "recorded_date": leg["recorded_date"],
                                  "ledger_rows": [], "evidence_id": leg["evidence_id"]})
        entry["amount_cents"] += leg["signed_cents"]
        entry["ledger_rows"].append(leg["ledger_row"])
    supported = {key for key in entries if ledger_basis_ok and not entry_issues[key]}
    timing_ids = {item["entry_id"] for item in timing}
    matched_bank, matched_entries, attempted_bank, attempted_entries = set(), set(), set(), set()
    matches = []
    for link in links:
        try:
            if not isinstance(link, dict) or set(link) != {"transaction_id", "entry_id"}:
                raise ValueError("each link requires transaction_id and entry_id only")
            bank_id = text(link["transaction_id"], "bank link.transaction_id")
            entry_id = text(link["entry_id"], "bank link.entry_id")
            if bank_id in attempted_bank or entry_id in attempted_entries:
                raise ValueError("a bank row or ledger entry is linked more than once")
            attempted_bank.add(bank_id); attempted_entries.add(entry_id)
            if bank_id not in bank or entry_id not in entries:
                raise ValueError("linked row or entry is not visible in the selected period and ledger view")
            if entry_id in timing_ids:
                raise ValueError("a matched entry cannot also be an outstanding timing item")
            if entry_id not in supported:
                raise ValueError("linked ledger entry lacks a supported retained basis")
            if bank[bank_id]["amount_cents"] != entries[entry_id]["amount_cents"]:
                raise ValueError(f"bank amount {dollars(bank[bank_id]['amount_cents'])} USD differs from ledger amount {dollars(entries[entry_id]['amount_cents'])} USD")
            matched_bank.add(bank_id); matched_entries.add(entry_id)
            matches.append({"transaction_id": bank_id, "entry_id": entry_id,
                            "bank_row": bank[bank_id], "ledger_entry": entries[entry_id],
                            "correspondence": "explicit_preparer_binding_checked_not_authenticated"})
        except (ValueError, TypeError) as exc:
            problems.append(issue("invalid_bank_match", links_id, str(exc)))
    settlements, group_ids, group_evidence = [], set(), set()
    for group in groups or []:
        group_subject = links_id
        try:
            if not isinstance(group, dict) or set(group) != {"id", "transaction_ids", "entry_ids", "evidence_id"}:
                raise ValueError("each settlement group requires id, transaction_ids, entry_ids and evidence_id only")
            group_id = text(group["id"], "settlement group.id")
            group_subject = group_id
            evidence_id = text(group["evidence_id"], "settlement group.evidence_id")
            if group_id in group_ids or evidence_id in group_evidence:
                raise ValueError("a settlement group ID or controlling evidence is used more than once")
            group_ids.add(group_id); group_evidence.add(evidence_id)
            bank_ids, entry_ids = group["transaction_ids"], group["entry_ids"]
            for label, ids in (("transaction_ids", bank_ids), ("entry_ids", entry_ids)):
                if not isinstance(ids, list) or not ids:
                    raise ValueError(f"settlement {label} requires a nonempty list")
                for member in ids: text(member, f"settlement {label} member")
                if len(ids) != len(set(ids)):
                    raise ValueError(f"settlement {label} contains duplicate members")
            if not ((len(bank_ids) == 1 and len(entry_ids) > 1) or (len(entry_ids) == 1 and len(bank_ids) > 1)):
                raise ValueError("settlements support one bank row to several full ledger entries, or several bank rows to one full entry")
            if attempted_bank.intersection(bank_ids) or attempted_entries.intersection(entry_ids):
                raise ValueError("a settlement member is also used in another match or group")
            attempted_bank.update(bank_ids); attempted_entries.update(entry_ids)
            if any(key not in bank for key in bank_ids) or any(key not in entries for key in entry_ids):
                raise ValueError("settlement members must be visible in the selected bank period and ledger view")
            if timing_ids.intersection(entry_ids):
                raise ValueError("settlement entries cannot also be outstanding timing items")
            if any(key not in supported or entries[key]["amount_cents"] == 0 for key in entry_ids):
                raise ValueError("each full settlement entry requires a supported nonzero cash basis")
            bank_total = sum(bank[key]["amount_cents"] for key in bank_ids)
            ledger_total = sum(entries[key]["amount_cents"] for key in entry_ids)
            if bank_total == 0 or bank_total != ledger_total:
                raise ValueError("settlement bank and ledger totals must agree exactly and be nonzero; no difference is allocated or plugged")
            if len(bank_ids) > 1 and any(bank[key]["amount_cents"] * bank_total <= 0 for key in bank_ids):
                raise ValueError("split bank rows must have the same direction as their full ledger entry")
            evidence = _source(files, evidence_id)
            support = json_object(evidence["content"])
            required = {"schema_version", "subject", "group_id", "basis_sha256", "unit", "bank_members", "ledger_members", "rationale"}
            if (set(support) != required or support.get("schema_version") != "accounting-bank-settlement/1"
                    or support.get("subject") != claim["id"] or support.get("group_id") != group_id
                    or support.get("basis_sha256") != basis or support.get("unit") != "USD"):
                raise ValueError("settlement evidence must bind this claim, group, currency and current export/mapping/reporting basis")
            rationale = text(support["rationale"], "settlement evidence.rationale")
            for label, id_field, ids, available in (("bank_members", "transaction_id", bank_ids, bank),
                                                    ("ledger_members", "entry_id", entry_ids, entries)):
                members = support[label]
                if not isinstance(members, list) or len(members) != len(ids):
                    raise ValueError(f"settlement evidence {label} must cover every group member exactly")
                retained = {}
                for member in members:
                    if not isinstance(member, dict) or set(member) != {id_field, "amount"}:
                        raise ValueError(f"settlement evidence {label} requires exact member IDs and amounts")
                    key = text(member[id_field], f"settlement evidence.{id_field}")
                    if key in retained: raise ValueError(f"settlement evidence {label} repeats a member")
                    retained[key] = money(member["amount"])
                if retained != {key: available[key]["amount_cents"] for key in ids}:
                    raise ValueError(f"settlement evidence {label} differs from the exact selected members or their individual amounts")
            matched_bank.update(bank_ids); matched_entries.update(entry_ids)
            settlements.append({"id": group_id, "evidence_id": evidence_id,
                                "evidence_sha256": evidence["actual_sha256"], "rationale": rationale,
                                "bank_rows": [bank[key] for key in bank_ids],
                                "ledger_entries": [entries[key] for key in entry_ids],
                                "bank_total_cents": bank_total, "ledger_total_cents": ledger_total,
                                "correspondence": "explicit_preparer_binding_checked_not_authenticated"})
        except (ValueError, TypeError) as exc:
            problems.append(issue("invalid_bank_settlement", group_subject, str(exc)))
    unmatched_bank = []
    for bank_id, row in bank.items():
        if bank_id in matched_bank:
            continue
        candidates = sorted(entry_id for entry_id in supported - matched_entries - timing_ids
                            if entries[entry_id]["amount_cents"] == row["amount_cents"])
        unmatched_bank.append({**row, "candidate_ledger_entries": candidates,
                               "candidate_status": "suggestions_only"})
        code = "ambiguous_bank_match" if len(candidates) > 1 else "unmatched_bank_transaction"
        problems.append(issue(code, bank_id, "Retained bank row has no supported explicit ledger binding; amount-only candidates are suggestions."))
    unmatched_entries = [entries[key] for key in sorted(entries.keys() - matched_entries - timing_ids)]
    for entry in unmatched_entries:
        problems.append(issue("unmatched_ledger_cash_entry", entry["entry_id"],
                              "Visible cash entry needs a supported bank link or separately supported timing evidence."))
    # Distinct row IDs do not prove that repeated equal payments represent different business events.
    candidate_users = {}
    for row in unmatched_bank:
        for candidate in row["candidate_ledger_entries"]:
            candidate_users.setdefault(candidate, []).append(row["transaction_id"])
    for candidate, bank_ids in candidate_users.items():
        if len(bank_ids) > 1:
            problems.append(issue("ambiguous_bank_match", candidate,
                                  "Multiple bank rows suggest the same ledger entry: " + ", ".join(bank_ids)))
    activity = sum(row["amount_cents"] for row in selected) if rows is not None else None
    calculated = opening + activity if opening is not None and activity is not None else None
    difference = ending - calculated if ending is not None and calculated is not None else None
    if opening is None or ending is None:
        problems.append(issue("bank_bridge_evidence_unavailable", claim["id"], "Retain both controlling bank opening and ending evidence."))
    if difference:
        problems.append(issue("bank_rollforward_difference", ident,
                              f"Reported ending minus opening-plus-bank-rows is {dollars(difference)} USD; no row or plug is invented."))
    facts = {"status": "unresolved" if problems else "reconciled", "completeness": "provided_extract_only",
             "binding_basis_sha256": basis, "binding_basis_ready": basis_ready,
             "bank_recording_history": "not_supplied", "retained_record_count": len(rows) if rows is not None else None,
             "selected_record_count": len(selected) if rows is not None else None,
             "bank_opening_cents": opening, "bank_activity_cents": activity,
             "calculated_bank_ending_cents": calculated, "reported_bank_ending_cents": ending,
             "bank_rollforward_difference_cents": difference, "matches": matches,
             "unmatched_bank_rows": unmatched_bank, "unmatched_ledger_entries": unmatched_entries,
             "timing_ledger_entries": sorted(timing_ids), "excluded_bank_rows": excluded,
             "normalization": {"mapping_id": mapping_id, "rules": mapping,
                               "normalized_bank_sha256": normalized_hash}}
    if groups is not None:
        facts["settlement_groups"] = settlements
    return facts, problems
