#!/usr/bin/env python3
"""Read-only accounting evidence: pin inputs, explain amounts, verify packs, compare versions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path

from acfr_factory.accounting_evidence import (SCHEMA, json_object, pack_for, review_markdown,
                                            validate_manifest, verify_pack, recheck_pack, evaluator_descriptor,
                                            dollars, source_refs)
from acfr_factory.link_contract import fingerprint
from acfr_factory.accounting_review import notes_template, review_notes, notes_markdown
from acfr_factory.accounting_view import review_html


def write_new(path: Path, content: str) -> None:
    write_new_bytes(path, content.encode("utf-8"))


def write_new_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"existing output differs; choose a new version path: {path}")
        return
    with path.open("xb") as stream:
        stream.write(content)


def encoded(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pin = sub.add_parser("pin", help="Hash the declared files into a NEW manifest after source review")
    pin.add_argument("manifest", type=Path)
    pin.add_argument("--output", required=True, type=Path)
    explain = sub.add_parser("explain", help="Retain inputs and produce a review plus open exceptions")
    explain.add_argument("manifest", type=Path)
    explain.add_argument("--out", required=True, type=Path)
    verify = sub.add_parser("verify", help="Recompute a retained pack against a separately retained digest")
    verify.add_argument("pack", type=Path)
    verify.add_argument("--expected-sha256", required=True)
    compare = sub.add_parser("compare", help="Compare retained claim amounts and support status")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--before-sha256", required=True)
    compare.add_argument("--after-sha256", required=True)
    template = sub.add_parser("review-template", help="Draft editable next-action notes for a verified pack")
    template.add_argument("pack", type=Path)
    template.add_argument("--expected-sha256", required=True)
    template.add_argument("--output", required=True, type=Path)
    review = sub.add_parser("review", help="Bind reviewer classifications and next actions; keep evidence failures open")
    review.add_argument("pack", type=Path)
    review.add_argument("notes", type=Path)
    review.add_argument("--expected-sha256", required=True)
    review.add_argument("--out", required=True, type=Path)
    bank_template = sub.add_parser("bank-links-template", help="Draft version-bound bank links; amount candidates remain suggestions")
    bank_template.add_argument("manifest", type=Path)
    bank_template.add_argument("--claim", required=True)
    bank_template.add_argument("--output", required=True, type=Path)
    view = sub.add_parser("view", help="Create an offline review page from a verified retained pack")
    view.add_argument("pack", type=Path)
    view.add_argument("--expected-sha256", required=True)
    view.add_argument("--output", required=True, type=Path)
    view.add_argument("--notes", type=Path)
    ar_template = sub.add_parser("receivables-template", help="Draft an empty source-bound invoice movement schedule")
    ar_template.add_argument("manifest", type=Path)
    ar_template.add_argument("--claim", required=True)
    ar_template.add_argument("--output", required=True, type=Path)
    recheck = sub.add_parser("recheck", help="Preserve a sealed report and evaluate its retained inputs under current local rules")
    recheck.add_argument("pack", type=Path)
    recheck.add_argument("--expected-sha256", required=True)
    recheck.add_argument("--out", required=True, type=Path)
    return parser


def load_verified(path, expected):
    return verify_pack(json_object(path.read_text(encoding="utf-8")), expected)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "pin":
            source = args.manifest.resolve()
            destination = args.output.resolve()
            if source == destination:
                raise ValueError("pin requires a new manifest path; preserve the earlier version")
            manifest = json_object(source.read_text(encoding="utf-8"))
            validate_manifest(manifest)
            for ident, ref in source_refs(manifest):
                path = Path(ref["path"]).expanduser()
                if not path.is_absolute():
                    path = source.parent / path
                ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                ref["path"] = os.path.relpath(path.resolve(), destination.parent)
            write_new(destination, encoded(manifest))
            result = {"status": "pinned", "manifest": str(destination), "source_authenticity": "not_verified"}
        elif args.command == "explain":
            pack = pack_for(args.manifest.resolve())
            ident = fingerprint(pack)
            out = args.out.resolve() / ident
            write_new(out / "pack.json", encoded(pack))
            write_new(out / "exceptions.json", encoded(pack["report"]["exception_register"]))
            write_new(out / "review.md", review_markdown(pack["report"]))
            html_content = review_html(pack, ident)
            html_digest = hashlib.sha256(html_content.encode()).hexdigest()
            html_path = out / "views" / html_digest / "review.html"
            write_new(html_path, html_content)
            supported = [c["id"] for c in pack["report"]["claims"] if c["status"] == "supported"]
            unresolved = [c["id"] for c in pack["report"]["claims"] if c["status"] == "unresolved"]
            result = {"status": pack["report"]["status"], "pack_sha256": ident, "output": str(out),
                      "review_html": str(html_path), "html_sha256": html_digest,
                      "supported_claims": supported, "unresolved_claims": unresolved,
                      "not_claimed": pack["report"]["not_claimed"]}
        elif args.command == "recheck":
            original_bytes = args.pack.read_bytes()
            original = json_object(original_bytes.decode("utf-8"))
            descriptor = evaluator_descriptor()
            current = recheck_pack(original, args.expected_sha256)
            if descriptor != evaluator_descriptor():
                raise ValueError("local evaluation sources changed during recheck; retry from a stable checkout")
            ident = fingerprint(current)
            receipt = {"schema_version": "accounting-recheck/1", "original_pack_sha256": args.expected_sha256,
                       "original_file_sha256": hashlib.sha256(original_bytes).hexdigest(),
                       "current_pack_sha256": ident, "retained_snapshot_sha256": fingerprint(current["snapshot"]),
                       "report_changed": original["report"] != current["report"],
                       "original_report_sha256": fingerprint(original["report"]),
                       "current_report_sha256": fingerprint(current["report"]),
                       "evaluator": descriptor, "evaluator_sha256": fingerprint(descriptor),
                       "authority": "current_retained_input_checks_only"}
            out = args.out.resolve() / ident
            html_content = review_html(current, ident)
            html_digest = hashlib.sha256(html_content.encode()).hexdigest()
            html_path = out / "views" / html_digest / "review.html"
            write_new(out / "pack.json", encoded(current))
            write_new(out / "exceptions.json", encoded(current["report"]["exception_register"]))
            write_new(out / "review.md", review_markdown(current["report"]))
            write_new(html_path, html_content)
            trail = out / "rechecks" / fingerprint(receipt)
            write_new_bytes(trail / "original-pack.json", original_bytes)
            write_new(trail / "receipt.json", encoded(receipt))
            result = {"status": current["report"]["status"], "operation": "rechecked", "output": str(out),
                      "pack_sha256": ident, "review_html": str(html_path), "html_sha256": html_digest,
                      "original_pack_sha256": args.expected_sha256, "report_changed": receipt["report_changed"],
                      "recheck_receipt": str(trail / "receipt.json"), "original_pack_copy": str(trail / "original-pack.json"),
                      "evaluator_sha256": receipt["evaluator_sha256"], "not_claimed": current["report"]["not_claimed"]}
        elif args.command == "verify":
            report = load_verified(args.pack, args.expected_sha256)
            result = {"status": "verified", "support_status": report["status"],
                      "scope": report["scope"], "not_claimed": report["not_claimed"]}
        elif args.command == "view":
            pack = json_object(args.pack.read_text(encoding="utf-8"))
            report = verify_pack(pack, args.expected_sha256)
            reviewed = None
            if args.notes:
                reviewed = review_notes(report, args.expected_sha256, json_object(args.notes.read_text(encoding="utf-8")))
            content = review_html(pack, args.expected_sha256, reviewed)
            write_new(args.output, content)
            result = {"status": report["status"], "operation": "view_written", "output": str(args.output),
                      "pack_sha256": args.expected_sha256, "html_sha256": hashlib.sha256(content.encode()).hexdigest()}
        elif args.command == "receivables-template":
            report = pack_for(args.manifest.resolve())["report"]
            claim = next((c for c in report["claims"] if c["id"] == args.claim), None)
            if claim is None or "receivables_reconciliation" not in claim:
                raise ValueError("choose a claim with declared invoice receivables inputs")
            ar = claim["receivables_reconciliation"]
            if not ar["binding_basis_ready"]:
                raise ValueError("review, pin and fix the ledger/population basis before drafting movements")
            template = {**ar["definition"], "basis_sha256": ar["binding_basis_sha256"], "movements": []}
            write_new(args.output, encoded(template))
            result = {"status": "template_written", "output": str(args.output),
                      "basis_sha256": ar["binding_basis_sha256"],
                      "visible_ledger_entries": [{key: row[key] for key in ("entry_id", "amount_cents", "effective_date", "recorded_date", "ledger_rows")}
                                                  for row in ar["entry_coverage"]],
                      "authority": "draft_only_no_automatic_allocations"}
        elif args.command == "bank-links-template":
            report = pack_for(args.manifest.resolve())["report"]
            claim = next((c for c in report["claims"] if c["id"] == args.claim), None)
            if claim is None or "bank_reconciliation" not in claim:
                raise ValueError("choose a claim with declared bank transaction inputs")
            bank = claim["bank_reconciliation"]
            if not bank["binding_basis_ready"]:
                raise ValueError("review, pin and fix the ledger/bank export basis before drafting links")
            template = {"schema_version": "accounting-bank-links/1", "subject": claim["id"],
                        "basis_sha256": bank["binding_basis_sha256"], "matches": []}
            write_new(args.output, encoded(template))
            result = {"status": "template_written", "output": str(args.output),
                      "basis_sha256": bank["binding_basis_sha256"],
                      "unmatched_bank_rows": bank["unmatched_bank_rows"],
                      "unmatched_ledger_entries": bank["unmatched_ledger_entries"],
                      "authority": "draft_only_no_automatic_matches"}
        elif args.command == "review-template":
            report = load_verified(args.pack, args.expected_sha256)
            write_new(args.output, encoded(notes_template(report, args.expected_sha256)))
            result = {"status": "template_written", "output": str(args.output),
                      "exception_count": len(report["exception_register"]["exceptions"])}
        elif args.command == "review":
            report = load_verified(args.pack, args.expected_sha256)
            notes = json_object(args.notes.read_text(encoding="utf-8"))
            reviewed = review_notes(report, args.expected_sha256, notes)
            ident = fingerprint(reviewed)
            out = args.out.resolve() / ident
            write_new(out / "review-notes.json", encoded(notes))
            write_new(out / "review-receipt.json", encoded(reviewed))
            write_new(out / "review.md", notes_markdown(report, reviewed))
            result = {"status": report["status"], "operation": "reviewed", "output": str(out),
                      "review_sha256": ident, "pack_sha256": args.expected_sha256,
                      "unannotated_exceptions": reviewed["unannotated_exceptions"],
                      "authority": reviewed["authority"]}
        else:
            before = load_verified(args.before, args.before_sha256)
            after = load_verified(args.after, args.after_sha256)
            if (before["scope"], before["unit"]) != (after["scope"], after["unit"]):
                raise ValueError("cannot compare different scopes or units")
            old = {c["id"]: c for c in before["claims"]}
            new = {c["id"]: c for c in after["claims"]}
            changes = []
            for ident in sorted(old.keys() | new.keys()):
                a, b = old.get(ident), new.get(ident)
                delta = b["ending_cents"] - a["ending_cents"] if a and b else None
                changes.append({"claim_id": ident, "before_status": a["status"] if a else "absent",
                                "after_status": b["status"] if b else "absent", "delta_cents": delta,
                                "delta_usd": dollars(delta) if delta is not None else None,
                                "mapping_changed": bool(a and b and (a["accounts"], a["normal_side"]) != (b["accounts"], b["normal_side"]))})
            coordinate = lambda report: {k: report[k] for k in ("period_start", "period_end", "known_at", "input_fingerprint")}
            result = {"status": "compared", "before_coordinate": coordinate(before),
                      "after_coordinate": coordinate(after), "changes": changes}
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        print(encoded({"status": "blocked", "reason": str(exc)}), end="")
        return 2
    print(encoded(result), end="")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
