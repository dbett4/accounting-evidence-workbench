"""Authored synthetic bank exports, explicit links, and timing checks."""
import copy
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acfr_factory.accounting_evidence import capture, evaluate_manifest, pack_for, verify_pack, review_markdown
from acfr_factory.accounting_bank import bank_link_basis
from acfr_factory.link_contract import fingerprint


class BankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.example = self.root / "example"
        shutil.copytree(ROOT / "examples/accounting-evidence", self.example)
        self.manifest = self.example / "manifest.bank.json"
        self.links = self.example / "sources/bank-links.json"
        self.export = self.example / "bank.export.csv"
        self.mapping = self.example / "bank.mapping.json"

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/accounting_evidence.py"), *map(str, args)],
                              text=True, capture_output=True, check=False)

    def repin(self, ident):
        manifest = json.loads(self.manifest.read_text())
        ref = manifest["ledger"] if ident == "ledger" else next(r for r in manifest["sources"] if r["id"] == ident)
        ref["sha256"] = hashlib.sha256((self.example / ref["path"]).read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))

    def rebind(self):
        snapshot = capture(self.manifest)
        basis, ready = bank_link_basis(snapshot["manifest"]["claims"][0], snapshot["manifest"],
                                       {r["id"]: r for r in snapshot["files"]})
        self.assertTrue(ready)
        self.edit_json(self.links, lambda links: links.update(basis_sha256=basis), "bank-links")

    def edit_json(self, path, edit, ident=None):
        value = json.loads(path.read_text()); edit(value); path.write_text(json.dumps(value))
        if ident: self.repin(ident)

    def alter_bank(self, edit, rebind=True):
        reader = csv.DictReader(io.StringIO(self.export.read_text())); fields, rows = reader.fieldnames, list(reader)
        edit(rows)
        output = io.StringIO(newline=""); writer = csv.DictWriter(output, fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows); self.export.write_text(output.getvalue())
        self.repin("bank-transactions")
        if rebind: self.rebind()

    def claims(self):
        return {c["id"]: c for c in evaluate_manifest(self.manifest)["claims"]}

    def assert_cash_unresolved(self, code):
        claims = self.claims()
        self.assertEqual(claims["cash"]["status"], "unresolved")
        self.assertEqual(claims["revenue"]["status"], "supported")
        self.assertIn(code, {item["code"] for item in claims["cash"]["issues"]})
        return claims["cash"]


    def add_timing(self):
        evidence = {"schema_version": "accounting-source/1", "kind": "timing_item", "subject": "pending-fee",
                    "unit": "USD", "amount": "100.00", "effective_date": "2026-03-31",
                    "entry_id": "fee", "timing_kind": "outstanding_payment"}
        schedule = {"schema_version": "accounting-reconciliation/1", "subject": "cash", "unit": "USD",
                    "effective_date": "2026-03-31", "items": [{"id": "pending-fee", "kind": "outstanding_payment",
                    "entry_id": "fee", "amount": "100.00", "evidence_id": "pending-fee"}]}
        manifest = json.loads(self.manifest.read_text())
        manifest["claims"][0]["reconciliation_evidence_id"] = "timing"
        for ident, doc in (("pending-fee", evidence), ("timing", schedule)):
            path = self.example / "sources" / (ident + ".json"); path.write_text(json.dumps(doc))
            manifest["sources"].append({"id": ident, "path": "sources/" + path.name,
                                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.manifest.write_text(json.dumps(manifest))

    def test_clean_bridge_and_all_explicit_rows_are_checked(self):
        cash = self.claims()["cash"]; bank = cash["bank_reconciliation"]
        self.assertEqual(cash["status"], "supported")
        self.assertEqual((bank["bank_opening_cents"], bank["bank_activity_cents"], bank["calculated_bank_ending_cents"]),
                         (1_000_000, 90_000, 1_090_000))
        self.assertEqual(bank["bank_rollforward_difference_cents"], 0)
        self.assertEqual((bank["retained_record_count"], bank["selected_record_count"], len(bank["matches"])), (3, 3, 3))
        self.assertEqual(bank["unmatched_bank_rows"], [])
        self.assertEqual(bank["unmatched_ledger_entries"], [])
        self.assertEqual(bank["matches"][1]["bank_row"]["source_line_start"], 3)
        self.assertEqual(bank["matches"][1]["bank_row"]["posted_date"], "2026-03-17")
        self.assertEqual(bank["matches"][1]["ledger_entry"]["effective_date"], "2026-03-15")
        self.assertEqual(bank["bank_recording_history"], "not_supplied")

    def test_link_basis_binds_exact_sources_coordinates_and_mapping(self):
        snapshot = capture(self.manifest); manifest = snapshot["manifest"]
        files = {r["id"]: r for r in snapshot["files"]}; claim = manifest["claims"][0]
        basis = {"sources": {key: files[key]["actual_sha256"] for key in
                              ("ledger", "bank-transactions", "ledger-mapping", "bank-mapping")},
                 "scope": "example-book-march-2026", "unit": "USD", "period_start": "2026-03-01",
                 "period_end": "2026-03-31", "known_at": "2026-04-16", "claim_id": "cash",
                 "accounts": ["1000"], "normal_side": "debit"}
        self.assertEqual(bank_link_basis(claim, manifest, files), (fingerprint(basis), True))

    def test_amount_only_candidates_are_never_automatically_accepted(self):
        self.edit_json(self.links, lambda doc: doc.update(matches=[]), "bank-links")
        cash = self.assert_cash_unresolved("unmatched_bank_transaction"); bank = cash["bank_reconciliation"]
        self.assertEqual(len(bank["matches"]), 0)
        self.assertEqual(bank["unmatched_bank_rows"][0]["candidate_ledger_entries"], ["receipt"])
        self.assertEqual(bank["unmatched_bank_rows"][0]["candidate_status"], "suggestions_only")
        self.assertEqual(len(bank["unmatched_ledger_entries"]), 3)
        self.assertEqual(bank["bank_rollforward_difference_cents"], 0)  # A numerical tie is insufficient.

    def test_unknown_or_malformed_or_duplicate_links_are_refused(self):
        original = self.links.read_text()
        edits = [lambda d: d["matches"][0].update(transaction_id="unknown"),
                 lambda d: d["matches"][0].update(entry_id="unknown"),
                 lambda d: d["matches"].append(copy.deepcopy(d["matches"][0])),
                 lambda d: d["matches"][1].update(entry_id="receipt"),
                 lambda d: d["matches"][0].update(accepted=True),
                 lambda d: d["matches"].append(None)]
        for edit in edits:
            with self.subTest(edit=edit):
                self.links.write_text(original); self.edit_json(self.links, edit, "bank-links")
                self.assert_cash_unresolved("invalid_bank_match")

    def test_unknown_link_schema_fields_and_wrong_subject_fail(self):
        original = self.links.read_text()
        for edit in (lambda d: d.update(subject="revenue"), lambda d: d.update(matchs=[]),
                     lambda d: d.pop("basis_sha256")):
            self.links.write_text(original); self.edit_json(self.links, edit, "bank-links")
            self.assert_cash_unresolved("invalid_bank_links")

    def test_equal_ids_and_amounts_do_not_allow_stale_links_after_source_changes(self):
        self.alter_bank(lambda rows: rows[0].update(Details="Changed transaction context"), rebind=False)
        self.assert_cash_unresolved("invalid_bank_links")
        self.rebind()
        self.assertEqual(self.claims()["cash"]["status"], "supported")

    def test_changed_ledger_mapping_or_bank_mapping_invalidates_link_basis(self):
        for path, ident in ((self.mapping, "bank-mapping"), (self.example / "ledger.mapping.json", "ledger-mapping")):
            original = path.read_text(); path.write_text(original + "\n")
            if ident == "ledger-mapping":
                manifest = json.loads(self.manifest.read_text())
                manifest["ledger"]["mapping"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.manifest.write_text(json.dumps(manifest))
            else: self.repin(ident)
            self.assert_cash_unresolved("invalid_bank_links")
            self.rebind()
            self.assertEqual(self.claims()["cash"]["status"], "supported")

    def test_unsupported_ledger_basis_or_entry_cannot_be_matched(self):
        (self.example / "sources/rent.json").unlink()
        self.assert_cash_unresolved("invalid_bank_match")
        ledger = self.example / "ledger.export.csv"; ledger.write_text(ledger.read_text() + "\n")
        claims = self.claims()
        self.assertTrue(all(c["status"] == "unresolved" for c in claims.values()))
        self.assertIn("source_revision_changed", {p["code"] for p in claims["cash"]["issues"]})
        self.assertFalse(claims["cash"]["bank_reconciliation"]["binding_basis_ready"])

    def test_bank_sources_revision_change_or_loss_only_blocks_cash(self):
        for path in (self.export, self.mapping, self.links, self.example / "sources/bank-opening.json"):
            with self.subTest(path=path):
                original = path.read_bytes(); path.write_bytes(original + b"\n")
                claims = self.claims()
                self.assertEqual(claims["cash"]["status"], "unresolved")
                self.assertEqual(claims["revenue"]["status"], "supported")
                path.unlink()
                self.assertEqual(self.claims()["cash"]["status"], "unresolved")
                path.write_bytes(original)

    def test_wrong_bank_opening_is_found_even_when_ledger_and_bank_ending_tie(self):
        self.edit_json(self.example / "sources/bank-opening.json", lambda d: d.update(amount="9900.00"), "bank-opening")
        cash = self.assert_cash_unresolved("bank_rollforward_difference")
        self.assertEqual(cash["bank_difference_cents"], 0)
        self.assertEqual(cash["bank_reconciliation"]["bank_rollforward_difference_cents"], 10_000)

    def test_wrong_bank_opening_kind_date_and_subject_remain_unresolved(self):
        path = self.example / "sources/bank-opening.json"; original = path.read_text()
        for edit in (lambda d: d.update(kind="opening_balance"), lambda d: d.update(subject="other"),
                     lambda d: d.update(effective_date="2026-03-02")):
            path.write_text(original); self.edit_json(path, edit, "bank-opening")
            self.assert_cash_unresolved("evidence_conflict")

    def test_signed_amount_mismatch_and_compensating_rows_do_not_disappear(self):
        self.alter_bank(lambda rows: (rows[0].update(Amount="2,499.99"), rows[1].update(Amount="-1,499.99")))
        cash = self.assert_cash_unresolved("invalid_bank_match")
        self.assertEqual(cash["bank_reconciliation"]["bank_rollforward_difference_cents"], 0)
        self.assertEqual(len(cash["bank_reconciliation"]["unmatched_bank_rows"]), 2)

    def test_duplicate_bank_ids_and_invalid_money_currency_dates_fail_closed(self):
        original = self.export.read_text()
        for edit in (lambda rows: rows[1].update(BankID="bank-receipt"),
                     lambda rows: rows[0].update(Amount="25,00.00"),
                     lambda rows: rows[0].update(Amount="2500.001"),
                     lambda rows: rows[0].update(Amount="2.5e3"),
                     lambda rows: rows[0].update(Amount=""),
                     lambda rows: rows[0].update(Amount="0.00"),
                     lambda rows: rows[0].update(Currency="EUR"),
                     lambda rows: rows[0].update(Date="03/08/26")):
            with self.subTest(edit=edit):
                self.export.write_text(original); self.alter_bank(edit)
                self.assert_cash_unresolved("invalid_bank_transactions")

    def test_bank_blank_amount_cannot_use_ledger_blank_zero_rule(self):
        self.edit_json(self.mapping, lambda m: m["columns"]["amount"].update(blank_as_zero=True), "bank-mapping")
        self.rebind()
        self.assert_cash_unresolved("invalid_bank_transactions")

    def test_truncated_csv_extra_columns_and_unknown_mapping_rules_fail(self):
        original = self.export.read_text()
        for suffix in ('"unfinished', "too,few\n", "a,b,c,d,e,f\n"):
            self.export.write_text(original + suffix); self.repin("bank-transactions"); self.rebind()
            self.assert_cash_unresolved("invalid_bank_transactions")

    def test_duplicate_equal_amount_rows_with_distinct_ids_remain_ambiguous(self):
        self.alter_bank(lambda rows: rows.append({**rows[0], "BankID": "another-receipt"}))
        self.edit_json(self.links, lambda d: d.update(matches=[]), "bank-links")
        cash = self.assert_cash_unresolved("ambiguous_bank_match")
        self.assertEqual(len(cash["bank_reconciliation"]["unmatched_bank_rows"]), 4)
        self.assertEqual(cash["bank_reconciliation"]["retained_record_count"], 4)

    def test_bank_rows_outside_period_are_retained_and_explicitly_excluded(self):
        self.alter_bank(lambda rows: rows.append({"BankID": "april-row", "Date": "04/01/2026", "Amount": "-42.00", "Currency": "USD", "Details": "Outside statement period"}))
        bank = self.claims()["cash"]["bank_reconciliation"]
        self.assertEqual(bank["status"], "reconciled")
        self.assertEqual((bank["retained_record_count"], bank["selected_record_count"]), (4, 3))
        self.assertEqual(bank["excluded_bank_rows"][0]["transaction_id"], "april-row")
        self.assertEqual(bank["bank_activity_cents"], 90_000)

    def test_links_to_excluded_bank_rows_are_invalid(self):
        self.alter_bank(lambda rows: rows[2].update(Date="04/01/2026"))
        self.assert_cash_unresolved("invalid_bank_match")

    def test_supported_outstanding_item_covers_unmatched_ledger_entry_without_plug(self):
        self.add_timing(); self.alter_bank(lambda rows: rows.pop(2))
        self.edit_json(self.links, lambda d: d["matches"].pop(2), "bank-links")
        self.edit_json(self.example / "sources/bank-cash.json", lambda d: d.update(amount="11000.00"), "bank-cash")
        cash = self.claims()["cash"]; bank = cash["bank_reconciliation"]
        self.assertEqual(cash["status"], "supported")
        self.assertEqual(cash["ending_cents"], 1_090_000)
        self.assertEqual(cash["bank_adjustment_cents"], -10_000)
        self.assertEqual((bank["reported_bank_ending_cents"], bank["timing_ledger_entries"]), (1_100_000, ["fee"]))
        self.assertEqual(bank["unmatched_ledger_entries"], [])

    def test_matched_bank_entry_cannot_also_be_a_timing_item(self):
        self.add_timing()
        self.assert_cash_unresolved("invalid_bank_match")

    def test_missing_timing_evidence_does_not_cover_an_unmatched_entry(self):
        self.add_timing(); self.alter_bank(lambda rows: rows.pop(2))
        self.edit_json(self.links, lambda d: d["matches"].pop(2), "bank-links")
        (self.example / "sources/pending-fee.json").unlink()
        self.assert_cash_unresolved("unmatched_ledger_cash_entry")

    def test_historical_ledger_view_does_not_invent_bank_recording_history(self):
        earlier = pack_for(self.manifest); earlier_digest = fingerprint(earlier)
        self.edit_json(self.manifest, lambda m: m.update(known_at="2026-04-14")); self.rebind()
        historical = self.assert_cash_unresolved("invalid_bank_match")
        self.assertEqual(historical["ending_cents"], 1_100_000)
        self.assertEqual(historical["bank_reconciliation"]["bank_recording_history"], "not_supplied")
        self.assertEqual(verify_pack(earlier, earlier_digest)["claims"][0]["ending_cents"], 1_090_000)

    def test_original_files_can_disappear_and_replay_checks_bank_facts(self):
        pack = pack_for(self.manifest); digest = fingerprint(pack)
        shutil.rmtree(self.example)
        self.assertEqual(verify_pack(pack, digest), pack["report"])
        pack["report"]["claims"][0]["bank_reconciliation"]["matches"] = []
        with self.assertRaisesRegex(ValueError, "recomputation"):
            verify_pack(pack, fingerprint(pack))

    def test_source_lines_boms_and_crlf_are_retained(self):
        self.alter_bank(lambda rows: rows[1].update(Details="Rent\nPayment"))
        raw = b"\xef\xbb\xbf" + self.export.read_bytes().replace(b"\n", b"\r\n")
        self.export.write_bytes(raw); self.repin("bank-transactions"); self.rebind()
        pack = pack_for(self.manifest)
        source = next(r for r in pack["snapshot"]["files"] if r["id"] == "bank-transactions")
        self.assertEqual(source["content"].encode("utf-8"), raw)
        row = pack["report"]["claims"][0]["bank_reconciliation"]["matches"][1]["bank_row"]
        self.assertEqual((row["source_line_start"], row["source_line_end"]), (3, 4))

    def test_bank_text_cannot_inject_active_markdown(self):
        self.alter_bank(lambda rows: rows[0].update(Details='<script>x</script> [go](javascript:alert(1))'))
        md = review_markdown(evaluate_manifest(self.manifest))
        self.assertNotIn("<script>", md); self.assertNotIn("[go](javascript", md)

    def test_normalized_bank_csv_without_a_column_map_is_supported(self):
        self.export.write_text('transaction_id,posted_date,amount,unit,reference\nbank-receipt,2026-03-08,2500.00,USD,Receipt\nbank-rent,2026-03-17,-1500.00,USD,Rent\nbank-fee,2026-03-31,-100.00,USD,Fee\n')
        self.edit_json(self.manifest, lambda m: m["claims"][0].pop("bank_transactions_mapping_id"))
        self.repin("bank-transactions"); self.rebind()
        self.assertEqual(self.claims()["cash"]["status"], "supported")

    def test_partial_bank_manifest_requirement_cannot_bypass_evaluation(self):
        self.edit_json(self.manifest, lambda m: m["claims"][0].pop("bank_links_evidence_id"))
        with self.assertRaises(ValueError): evaluate_manifest(self.manifest)


    def test_cli_drafts_empty_source_bound_links_and_reports_unaccepted_candidates(self):
        self.edit_json(self.links, lambda d: d.update(matches=[]), "bank-links")
        output = self.root / "new-links.json"
        proc = self.cli("bank-links-template", self.manifest, "--claim", "cash", "--output", output)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        template = json.loads(output.read_text()); result = json.loads(proc.stdout)
        self.assertEqual(template["matches"], [])
        self.assertEqual(template["basis_sha256"], result["basis_sha256"])
        self.assertEqual(len(result["unmatched_bank_rows"]), 3)
        self.assertEqual(self.claims()["cash"]["status"], "unresolved")
        self.export.write_text(self.export.read_text() + "\n")
        self.assertEqual(self.cli("bank-links-template", self.manifest, "--claim", "cash", "--output", self.root / "next.json").returncode, 2)

    def test_cli_replayed_version_comparison_and_pin_paths(self):
        before = self.cli("explain", self.manifest, "--out", self.root / "packs")
        self.assertEqual(before.returncode, 0, before.stdout); old = json.loads(before.stdout)
        new_manifest = self.root / "new/manifest.json"
        self.assertEqual(self.cli("pin", self.manifest, "--output", new_manifest).returncode, 0)
        self.assertEqual(evaluate_manifest(new_manifest)["status"], "pass")
        self.edit_json(self.links, lambda d: d.update(matches=[]), "bank-links")
        after = self.cli("explain", self.manifest, "--out", self.root / "packs")
        self.assertEqual(after.returncode, 1); new = json.loads(after.stdout)
        compared = self.cli("compare", Path(old["output"]) / "pack.json", Path(new["output"]) / "pack.json",
                            "--before-sha256", old["pack_sha256"], "--after-sha256", new["pack_sha256"])
        self.assertEqual(compared.returncode, 0)
        self.assertEqual(json.loads(compared.stdout)["changes"][0]["after_status"], "unresolved")


if __name__ == "__main__": unittest.main()
