"""Retained gross invoice populations and explicitly evidenced ledger movements."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date

from .accounting_evidence import dollars, issue, json_object, money, text
from .accounting_source_mapping import RECEIVABLES_COLUMNS, normalize_receivables_export
from .link_contract import fingerprint


def _source(files, ident):
    record = files.get(ident)
    if record is None or record["problem"] or record["content"] is None:
        raise ValueError(f"{ident}: {record['problem'] if record else 'undeclared source'}")
    return record


def receivables_basis(claim, manifest, files, definition):
    ids = ["ledger", definition["opening_population_id"], definition["ending_population_id"]]
    if "mapping" in manifest["ledger"]: ids.append("ledger-mapping")
    ids += [definition[key] for key in ("opening_mapping_id", "ending_mapping_id") if key in definition]
    basis = {"sources": {ident: files.get(ident, {}).get("actual_sha256") for ident in ids},
             "scope": manifest["scope"], "unit": manifest["unit"], "claim_id": claim["id"],
             "accounts": claim["accounts"], "normal_side": claim["normal_side"],
             "period_start": manifest["period_start"], "period_end": manifest["period_end"],
             "known_at": manifest["known_at"]}
    return fingerprint(basis), all(ident in files and not files[ident]["problem"] for ident in ids)


def _population(files, ident, mapping_id, cutoff):
    record = _source(files, ident)
    mapping = (json_object(_source(files, mapping_id)["content"]) if mapping_id else
               {"schema_version": "accounting-receivables-column-map/1",
                "columns": {key: {"column": key} for key in RECEIVABLES_COLUMNS}, "ignored_columns": []})
    normalized, lineage = normalize_receivables_export(record["content"], mapping)
    invoices = {}
    for row, origin in zip(csv.DictReader(io.StringIO(normalized)), lineage, strict=True):
        ident = text(row["invoice_id"], "population.invoice_id")
        text(row["customer_id"], "population.customer_id")
        amount = money(row["amount"])
        if ident in invoices or row["unit"] != "USD" or amount < 0:
            raise ValueError(f"population record {origin['source_record']}: duplicate invoice, unsupported currency or negative balance")
        issued, due = date.fromisoformat(row["issue_date"]), date.fromisoformat(row["due_date"])
        if issued > cutoff or due < issued:
            raise ValueError(f"population record {origin['source_record']}: issue date exceeds snapshot date or due date precedes issue")
        invoices[ident] = {"invoice_id": ident, "customer_id": row["customer_id"],
                           "issue_date": row["issue_date"], "due_date": row["due_date"],
                           "amount_cents": amount, **origin, "source_revision": record["actual_sha256"]}
    return invoices, {"mapping_id": mapping_id, "rules": mapping,
                      "normalized_population_sha256": hashlib.sha256(normalized.encode()).hexdigest()}


def reconcile_receivables(claim, manifest, files, trace, entry_issues, opening, ending, *, ledger_basis_ok):
    problems = []
    schedule_id = claim["receivables_evidence_id"]
    facts = {"status": "unresolved", "completeness": "provided_populations_only",
             "population_recording_history": "not_supplied", "binding_basis_sha256": None,
             "binding_basis_ready": False, "definition": None, "normalization": {},
             "opening_population_cents": None, "ending_population_cents": None,
             "opening_minus_ledger_cents": None, "ending_minus_ledger_cents": None,
             "allocated_activity_cents": None, "retained_movement_count": None,
             "parsed_movement_count": 0, "movements": [], "invalid_movements": [], "entry_coverage": [], "invoices": [],
             "aging_cents": None, "correspondence": "explicit_preparer_bindings_checked_not_authenticated"}
    def fail(code, subject, detail): problems.append(issue(code, subject, detail))
    try:
        definition = json_object(_source(files, schedule_id)["content"])
        required = {"schema_version", "scope", "subject", "unit", "period_start", "period_end", "known_at",
                    "basis_sha256", "opening_population_id", "ending_population_id", "movements"}
        if set(definition) - {"opening_mapping_id", "ending_mapping_id"} != required:
            raise ValueError("receivables schedule requires reporting coordinates, populations, basis and explicit movements only")
        expected = {key: manifest[key] for key in ("scope", "unit", "period_start", "period_end", "known_at")}
        expected.update(schema_version="accounting-receivables/1", subject=claim["id"])
        if any(definition[key] != value for key, value in expected.items()) or not isinstance(definition["movements"], list):
            raise ValueError("receivables schedule subject, reporting coordinates or movement list disagree")
        for key in ("opening_population_id", "ending_population_id", "opening_mapping_id", "ending_mapping_id"):
            if key in definition: text(definition[key], key)
        facts["definition"] = definition
    except (ValueError, TypeError) as exc:
        fail("invalid_receivables_schedule", schedule_id, str(exc)); return facts, problems
    populations = {}
    for label, cutoff in (("opening", manifest["period_start"]), ("ending", manifest["period_end"])):
        ident = definition[label + "_population_id"]
        try:
            populations[label], rules = _population(files, ident, definition.get(label + "_mapping_id"), date.fromisoformat(cutoff))
            facts["normalization"][label] = rules
            total = sum(row["amount_cents"] for row in populations[label].values())
            facts[label + "_population_cents"] = total
            difference = total - (opening if label == "opening" else ending)
            facts[label + "_minus_ledger_cents"] = difference
            if difference:
                fail("receivables_population_difference", ident, f"{label.capitalize()} population minus ledger is {dollars(difference)} USD; no invoice or plug is invented.")
        except (ValueError, TypeError, csv.Error) as exc:
            fail("invalid_receivables_population", ident, str(exc))
    basis, ready = receivables_basis(claim, manifest, files, definition)
    ready = ready and ledger_basis_ok and len(populations) == 2
    if not ledger_basis_ok:
        fail("unsupported_receivables_basis", schedule_id, "Resolve the retained ledger and mapping basis before accepting invoice coverage.")
    facts.update(binding_basis_sha256=basis, binding_basis_ready=ready,
                 retained_movement_count=len(definition["movements"]))
    fresh = definition["basis_sha256"] == basis
    if not fresh:
        fail("stale_receivables_binding", schedule_id, "Schedule belongs to different exports, mappings or reporting coordinates; prepare a reviewed new version.")
    entries = {}
    for leg in trace:
        entry = entries.setdefault(leg["entry_id"], {"entry_id": leg["entry_id"], "amount_cents": 0,
                "effective_date": leg["effective_date"], "recorded_date": leg["recorded_date"],
                "ledger_rows": [], "evidence_id": leg["evidence_id"]})
        entry["amount_cents"] += leg["signed_cents"]; entry["ledger_rows"].append(leg["ledger_row"])
    invoice_model = {key: dict(row, opening_cents=row["amount_cents"]) for key, row in populations.get("opening", {}).items()}
    seen, used_evidence = set(), set()
    metadata, invalid_invoices = {}, set()
    for ordinal, raw in enumerate(definition["movements"], 1):
        subject = schedule_id
        try:
            if not isinstance(raw, dict) or set(raw) != {"id", "kind", "invoice_id", "entry_id", "amount", "evidence_id"}:
                raise ValueError("each movement requires id, kind, invoice_id, entry_id, amount and evidence_id only")
            for key in ("id", "invoice_id", "entry_id", "evidence_id"): text(raw[key], "movement." + key)
            subject = raw["id"]
            if raw["id"] in seen or raw["evidence_id"] in used_evidence:
                raise ValueError("movement ID or controlling movement evidence is used more than once")
            seen.add(raw["id"]); used_evidence.add(raw["evidence_id"])
            amount = money(raw["amount"])
            if raw["kind"] not in {"invoice", "collection"} or amount == 0 or ((raw["kind"] == "invoice") != (amount > 0)):
                raise ValueError("only positive invoice and negative collection movements are supported")
            if raw["entry_id"] not in entries:
                raise ValueError("movement entry is not visible in the selected ledger period and recorded view")
            entry = entries[raw["entry_id"]]
            row = {key: raw[key] for key in ("id", "kind", "invoice_id", "entry_id", "evidence_id")}
            row.update(amount_cents=amount, effective_date=entry["effective_date"], recorded_date=entry["recorded_date"],
                       status="unresolved", source_record=ordinal, evidence_sha256=files.get(raw["evidence_id"], {}).get("actual_sha256"))
            facts["movements"].append(row)
            support = json_object(_source(files, raw["evidence_id"])["content"])
            expected = {"schema_version": "accounting-receivable-movement/1", "subject": raw["id"],
                        "kind": raw["kind"], "unit": "USD", "invoice_id": raw["invoice_id"],
                        "entry_id": raw["entry_id"], "effective_date": entry["effective_date"],
                        "recorded_date": entry["recorded_date"], "basis_sha256": basis}
            if set(support) != set(expected) | {"amount", "customer_id", "issue_date", "due_date", "rationale"} or any(support[key] != value for key, value in expected.items()):
                raise ValueError("movement evidence must bind this invoice, full entry, dates, kind, currency and current basis")
            if money(support["amount"]) != amount:
                raise ValueError("movement evidence differs from the individual exact signed allocation")
            text(support["customer_id"], "movement evidence.customer_id"); text(support["rationale"], "movement evidence.rationale")
            # Invoice metadata uses canonical ISO dates; ledger representations remain retained.
            for key in ("issue_date", "due_date"):
                value = support[key]
                if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("movement invoice dates require YYYY-MM-DD")
            if support["due_date"] < support["issue_date"]:
                raise ValueError("movement invoice due date precedes issue date")
            metadata[row["id"]] = {key: support[key] for key in ("customer_id", "issue_date", "due_date")}
            row["status"] = "supported" if fresh and ready else "unresolved"
        except (ValueError, TypeError) as exc:
            fail("invalid_receivable_movement", subject, str(exc))
            invoice_id = raw.get("invoice_id") if isinstance(raw, dict) else None
            if isinstance(invoice_id, str): invalid_invoices.add(invoice_id)
            facts["invalid_movements"].append({"source_record": ordinal, "subject": subject,
                                               "invoice_id": invoice_id if isinstance(invoice_id, str) else None,
                                               "detail": str(exc)})
    facts["parsed_movement_count"] = len(facts["movements"])
    facts["allocated_activity_cents"] = sum(row["amount_cents"] for row in facts["movements"])
    by_entry, by_invoice = {}, {}
    for row in facts["movements"]:
        by_entry.setdefault(row["entry_id"], []).append(row)
        by_invoice.setdefault(row["invoice_id"], []).append(row)
    # Build the population before checking collections so file order does not invent intraday order.
    for row in facts["movements"]:
        if row["kind"] != "invoice" or row["id"] not in metadata: continue
        invoice_id = row["invoice_id"]
        if invoice_id in invoice_model or date.fromisoformat(metadata[row["id"]]["issue_date"]) != date.fromisoformat(row["effective_date"]):
            row["status"] = "unresolved"
            fail("invalid_receivable_invoice", invoice_id, "An invoice movement must introduce one new invoice at its stated issue date.")
        else:
            invoice_model[invoice_id] = {"invoice_id": invoice_id, **metadata[row["id"]], "opening_cents": 0}
    for row in facts["movements"]:
        model = invoice_model.get(row["invoice_id"])
        if model is None:
            row["status"] = "unresolved"; fail("unknown_receivable_invoice", row["invoice_id"], "Movement names no retained opening invoice or supported new invoice definition.")
        elif row["id"] in metadata and (metadata[row["id"]] != {key: model[key] for key in metadata[row["id"]]} or date.fromisoformat(row["effective_date"]) < date.fromisoformat(model["issue_date"])):
            row["status"] = "unresolved"; fail("receivable_identity_conflict", row["invoice_id"], "Movement party/terms or effective date disagree with the retained invoice identity.")
    for key, entry in entries.items():
        allocations = by_entry.get(key, [])
        total = sum(row["amount_cents"] for row in allocations)
        supported = (ledger_basis_ok and not entry_issues[key] and entry["amount_cents"] != 0
                     and total == entry["amount_cents"] and bool(allocations)
                     and all(row["status"] == "supported" and row["amount_cents"] * entry["amount_cents"] > 0 for row in allocations))
        facts["entry_coverage"].append({**entry, "allocated_cents": total, "difference_cents": total - entry["amount_cents"],
                                        "movement_ids": [row["id"] for row in allocations], "status": "supported" if supported else "unresolved"})
        if not supported:
            fail("receivable_entry_coverage", key, "Every visible full receivable entry needs supported same-direction allocations that sum exactly; no allocation is guessed.")
            for row in allocations: row["status"] = "unresolved"
    if "opening" not in populations: return facts, problems
    ending_population = populations.get("ending")
    invoice_ids = set(invoice_model) | set(ending_population or {})
    aging = {key: 0 for key in ("current", "1_30", "31_60", "61_90", "91_plus")}
    for key in sorted(invoice_ids):
        model = invoice_model.get(key); reported = (ending_population or {}).get(key)
        movements = by_invoice.get(key, [])
        expected = None if model is None else model["opening_cents"] + sum(row["amount_cents"] for row in movements)
        amount = reported["amount_cents"] if reported else 0 if ending_population is not None and expected == 0 else None
        difference = None if expected is None or amount is None else amount - expected
        identity_ok = model is not None and (reported is None or all(reported[field] == model[field] for field in ("customer_id", "issue_date", "due_date")))
        if model is None: fail("unknown_ending_receivable", key, "Ending invoice has no retained opening or supported issuance definition.")
        elif not identity_ok: fail("receivable_identity_conflict", key, "Ending party or invoice terms differ from the retained identity.")
        if expected is not None:
            daily = {}
            for row in movements:
                day = date.fromisoformat(row["effective_date"]).isoformat()
                daily[day] = daily.get(day, 0) + row["amount_cents"]
            balance = model["opening_cents"]
            for day in sorted(daily):
                balance += daily[day]
                if balance < 0: fail("receivable_overcollection", key, f"Declared day-end balance is negative on {day}; no credit balance or refund is inferred.")
            if ending_population is not None and amount is None:
                fail("missing_ending_receivable", key, "Invoice with a nonzero calculated balance is absent from the ending population.")
            if difference: fail("receivable_invoice_difference", key, f"Ending invoice minus opening-plus-movements is {dollars(difference)} USD.")
        terms = model or reported
        days = max(0, (date.fromisoformat(manifest["period_end"]) - date.fromisoformat(terms["due_date"])).days)
        bucket = "current" if days == 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "91_plus"
        if reported: aging[bucket] += reported["amount_cents"]
        item_ok = (ready and fresh and identity_ok and key not in invalid_invoices
                   and expected is not None and expected >= 0 and difference == 0
                   and all(row["status"] == "supported" for row in movements))
        facts["invoices"].append({"invoice_id": key, **{field: terms[field] for field in ("customer_id", "issue_date", "due_date")},
                                  "opening_cents": model["opening_cents"] if model else None,
                                  "movement_cents": sum(row["amount_cents"] for row in movements),
                                  "calculated_ending_cents": expected, "reported_ending_cents": amount,
                                  "difference_cents": difference, "movement_ids": [row["id"] for row in movements],
                                  "opening_record": populations["opening"].get(key), "ending_record": reported,
                                  "ending_presence": "retained" if reported else "settled_and_absent" if amount == 0 else "missing_or_unavailable",
                                  "overdue_days": days, "aging_bucket": bucket, "status": "supported" if item_ok else "unresolved"})
    facts["aging_cents"] = aging if ending_population is not None else None
    facts["status"] = "unresolved" if problems else "reconciled"
    return facts, problems
