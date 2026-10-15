"""Authored synthetic evidence regressions over real local files."""
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
from acfr_factory.accounting_evidence import (capture, evaluate_manifest, evaluate_snapshot,
                                            money, pack_for, review_markdown, verify_pack)
from acfr_factory.link_contract import fingerprint


class AccountingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.example = self.root / "example"
        shutil.copytree(ROOT / "examples" / "accounting-evidence", self.example)
        self.manifest = self.example / "manifest.json"

    def edit_manifest(self, edit):
        value = json.loads(self.manifest.read_text())
        edit(value)
        self.manifest.write_text(json.dumps(value))

    def repin(self, ident):
        value = json.loads(self.manifest.read_text())
        ref = value["ledger"] if ident == "ledger" else next(r for r in value["sources"] if r["id"] == ident)
        ref["sha256"] = hashlib.sha256((self.example / ref["path"]).read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(value))

    def claims(self):
        return {c["id"]: c for c in evaluate_manifest(self.manifest)["claims"]}

    def codes(self, claim):
        return {p["code"] for p in claim["issues"]}

    def alter_ledger(self, edit):
        path = self.example / "ledger.csv"
        reader = csv.DictReader(io.StringIO(path.read_text()))
        fields = reader.fieldnames
        rows = list(reader)
        edit(rows)
        output = io.StringIO()
        writer = csv.DictWriter(output, fields)
        writer.writeheader(); writer.writerows(rows)
        path.write_text(output.getvalue())
        self.repin("ledger")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/accounting_evidence.py"), *map(str, args)],
                              capture_output=True, text=True, check=False)


    def test_clean_files_agree_with_authored_bridge(self):
        claims = self.claims()
        self.assertEqual(claims["cash"]["status"], "supported")
        self.assertEqual((claims["cash"]["opening_cents"], claims["cash"]["activity_cents"],
                          claims["cash"]["ending_cents"]), (1_000_000, 90_000, 1_090_000))
        self.assertEqual(claims["cash"]["bank_difference_cents"], 0)
        self.assertEqual(claims["revenue"]["ending_cents"], 250_000)
        self.assertEqual(len(claims["cash"]["trace"]), 3)

    def test_missing_rent_only_blocks_dependent_cash_claim(self):
        (self.example / "sources/rent.json").unlink()
        claims = self.claims()
        self.assertEqual(claims["cash"]["status"], "unresolved")
        self.assertIn("missing_or_unreadable_source", self.codes(claims["cash"]))
        self.assertEqual(claims["cash"]["numerical_basis"], "provisional")
        self.assertEqual(claims["revenue"]["status"], "supported")

    def test_conflicting_re_pinned_source_blocks_cash_and_keeps_revenue(self):
        source = self.example / "sources/rent.json"
        doc = json.loads(source.read_text()); doc["amount"] = "1400.00"
        source.write_text(json.dumps(doc)); self.repin("rent")
        claims = self.claims()
        self.assertIn("evidence_conflict", self.codes(claims["cash"]))
        self.assertEqual(claims["revenue"]["status"], "supported")

    def test_source_revision_change_is_not_implicitly_accepted(self):
        with (self.example / "sources/rent.json").open("a") as stream: stream.write("\n")
        self.assertIn("source_revision_changed", self.codes(self.claims()["cash"]))

    def test_invalid_source_json_only_blocks_its_dependents(self):
        (self.example / "sources/rent.json").write_text("not JSON")
        self.repin("rent")
        claims = self.claims()
        self.assertIn("invalid_evidence_json", self.codes(claims["cash"]))
        self.assertEqual(claims["revenue"]["status"], "supported")

    def test_undeclared_required_evidence_is_unresolved(self):
        self.edit_manifest(lambda m: m["sources"].pop(3))
        self.assertIn("missing_evidence_reference", self.codes(self.claims()["cash"]))

    def test_missing_revision_is_not_assumed_current(self):
        self.edit_manifest(lambda m: m["sources"][3].pop("sha256"))
        self.assertIn("missing_or_invalid_source_revision", self.codes(self.claims()["cash"]))

    def test_one_cent_imbalance_blocks_affected_entry_claims(self):
        self.alter_ledger(lambda rows: rows[3].update(credit="1499.99"))
        claims = self.claims()
        self.assertIn("unbalanced_entry", self.codes(claims["cash"]))
        self.assertEqual(claims["revenue"]["status"], "supported")

    def test_duplicate_leg_is_not_silently_deduplicated(self):
        self.alter_ledger(lambda rows: rows.append(dict(rows[3])))
        self.assertIn("duplicate_leg", self.codes(self.claims()["cash"]))

    def test_malformed_row_cannot_disappear_from_denominator(self):
        self.alter_ledger(lambda rows: rows[3].update(account=""))
        for claim in self.claims().values():
            self.assertEqual(claim["status"], "unresolved")
            self.assertIn("invalid_ledger_row", self.codes(claim))

    def test_inconsistent_dates_across_entry_legs_fail_closed(self):
        self.alter_ledger(lambda rows: rows[3].update(recorded_date="2026-03-16"))
        self.assertTrue(all("entry_coordinate_conflict" in self.codes(c) for c in self.claims().values()))

    def test_future_recorded_fee_is_excluded_and_difference_remains(self):
        self.edit_manifest(lambda m: m.update(known_at="2026-04-01"))
        cash = self.claims()["cash"]
        self.assertEqual(cash["ending_cents"], 1_100_000)
        self.assertEqual(cash["bank_difference_cents"], -10_000)
        self.assertIn("unresolved_bank_difference", self.codes(cash))

    def test_old_pack_replays_after_sources_are_changed_and_removed(self):
        pack = pack_for(self.manifest); expected = fingerprint(pack)
        shutil.rmtree(self.example / "sources")
        report = verify_pack(pack, expected)
        self.assertEqual(report["claims"][0]["ending_cents"], 1_090_000)

    def test_false_saved_pass_does_not_survive_recomputation(self):
        (self.example / "sources/rent.json").unlink()
        pack = pack_for(self.manifest)
        pack["report"]["claims"][0]["status"] = "supported"
        with self.assertRaisesRegex(ValueError, "recomputation"):
            verify_pack(pack, fingerprint(pack))

    def test_retained_content_change_without_hash_change_is_rejected(self):
        pack = pack_for(self.manifest)
        pack["snapshot"]["files"][1]["content"] += "\n"
        with self.assertRaisesRegex(ValueError, "bytes disagree"):
            verify_pack(pack, fingerprint(pack))

    def test_changed_pack_is_rejected_against_separate_anchor(self):
        pack = pack_for(self.manifest); expected = fingerprint(pack)
        pack["report"]["claims"][0]["ending_cents"] += 1
        with self.assertRaisesRegex(ValueError, "independently retained"):
            verify_pack(pack, expected)

    def test_forged_source_problem_state_cannot_create_support(self):
        (self.example / "sources/rent.json").unlink()
        snapshot = capture(self.manifest)
        next(f for f in snapshot["files"] if f["id"] == "rent")["problem"] = None
        with self.assertRaisesRegex(ValueError, "problem state"):
            evaluate_snapshot(snapshot)

    def test_retained_source_metadata_cannot_be_rebound(self):
        snapshot = capture(self.manifest)
        snapshot["files"][1]["expected_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            evaluate_snapshot(snapshot)

    def test_utf8_bom_preserves_original_hash_and_replays(self):
        for name in ("ledger.csv", "sources/rent.json"):
            path = self.example / name; path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.repin("ledger"); self.repin("rent")
        pack = pack_for(self.manifest)
        self.assertEqual(verify_pack(pack, fingerprint(pack))["status"], "pass")

    def test_duplicate_json_fields_are_not_last_value_wins(self):
        source = self.example / "sources/rent.json"
        source.write_text('{"amount":"1400.00","amount":"1500.00"}')
        self.repin("rent")
        self.assertIn("invalid_evidence_json", self.codes(self.claims()["cash"]))

    def test_money_never_uses_floats_or_silent_rounding(self):
        self.assertEqual(money("-0.01"), -1)
        self.assertEqual(money("12345678901234567890.12"), 1234567890123456789012)
        for invalid in (1.1, True, None, "", "1.001", "1e3", "NaN", "1,000.00"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): money(invalid)

    def test_bank_date_and_subject_must_match_claim(self):
        source = self.example / "sources/bank-cash.json"
        doc = json.loads(source.read_text()); doc["effective_date"] = "2026-03-30"
        source.write_text(json.dumps(doc)); self.repin("bank-cash")
        cash = self.claims()["cash"]
        self.assertIn("evidence_conflict", self.codes(cash))
        self.assertIsNone(cash["bank_difference_cents"])

    def add_outstanding_payment(self):
        self.alter_ledger(lambda rows: rows.extend([
            {"entry_id": "payment", "leg_id": "1", "account": "6000", "debit": "200.00", "credit": "0.00",
             "unit": "USD", "effective_date": "2026-03-28", "recorded_date": "2026-03-28", "evidence_id": "payment"},
            {"entry_id": "payment", "leg_id": "2", "account": "1000", "debit": "0.00", "credit": "200.00",
             "unit": "USD", "effective_date": "2026-03-28", "recorded_date": "2026-03-28", "evidence_id": "payment"}]))
        docs = {
            "payment": {"schema_version": "accounting-source/1", "kind": "transaction", "subject": "payment", "amount": "200.00", "unit": "USD", "effective_date": "2026-03-28"},
            "timing": {"schema_version": "accounting-source/1", "kind": "timing_item", "subject": "check-1", "amount": "200.00", "unit": "USD", "effective_date": "2026-03-31", "entry_id": "payment", "timing_kind": "outstanding_payment"},
            "schedule": {"schema_version": "accounting-reconciliation/1", "subject": "cash", "unit": "USD", "effective_date": "2026-03-31", "items": [{"id": "check-1", "kind": "outstanding_payment", "entry_id": "payment", "amount": "200.00", "evidence_id": "timing"}]},
        }
        for ident, doc in docs.items():
            path = self.example / "sources" / (ident + ".json")
            path.write_text(json.dumps(doc))
            self.edit_manifest(lambda m, ident=ident, path=path: m["sources"].append({"id": ident, "path": "sources/" + path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
        self.edit_manifest(lambda m: m["claims"][0].update(reconciliation_evidence_id="schedule"))

    def test_supported_outstanding_payment_reconciles_without_changing_ledger(self):
        self.add_outstanding_payment()
        cash = self.claims()["cash"]
        self.assertEqual(cash["ending_cents"], 1_070_000)
        self.assertEqual(cash["bank_adjustment_cents"], -20_000)
        self.assertEqual(cash["bank_difference_cents"], 0)
        self.assertEqual(cash["status"], "supported")

    def test_timing_item_cannot_be_an_unsupported_plug(self):
        self.add_outstanding_payment()
        path = self.example / "sources/schedule.json"
        doc = json.loads(path.read_text()); doc["items"][0]["amount"] = "199.99"
        path.write_text(json.dumps(doc)); self.repin("schedule")
        cash = self.claims()["cash"]
        self.assertIn("invalid_timing_item", self.codes(cash))
        self.assertEqual(cash["bank_adjustment_cents"], 0)
        self.assertEqual(cash["status"], "unresolved")

    def test_timing_schedule_cannot_double_count_one_payment(self):
        self.add_outstanding_payment()
        path = self.example / "sources/schedule.json"
        doc = json.loads(path.read_text()); doc["items"].append({**doc["items"][0], "id": "check-2"})
        path.write_text(json.dumps(doc)); self.repin("schedule")
        self.assertIn("invalid_timing_item", self.codes(self.claims()["cash"]))

    def test_missing_timing_evidence_blocks_only_cash(self):
        self.add_outstanding_payment()
        (self.example / "sources/timing.json").unlink()
        claims = self.claims()
        self.assertEqual(claims["cash"]["status"], "unresolved")
        self.assertEqual(claims["revenue"]["status"], "supported")

    def test_misspelled_requirement_is_not_silently_ignored(self):
        self.edit_manifest(lambda m: m["claims"][0].update(bank_evience_id="bank-cash"))
        with self.assertRaisesRegex(ValueError, "unknown claim field"):
            evaluate_manifest(self.manifest)

    def test_duplicate_claim_mapping_refused(self):
        self.edit_manifest(lambda m: m["claims"][0]["accounts"].append("1000"))
        with self.assertRaisesRegex(ValueError, "duplicate account"):
            evaluate_manifest(self.manifest)

    def test_unknown_mapping_account_cannot_silently_produce_supported_zero(self):
        self.edit_manifest(lambda m: m["claims"][1].update(accounts=["missing-account"]))
        claims = self.claims()
        self.assertIn("unknown_mapping_account", self.codes(claims["revenue"]))
        self.assertEqual(claims["cash"]["status"], "supported")

    def test_opening_evidence_binds_the_claim_mapping(self):
        self.edit_manifest(lambda m: m["claims"][1].update(accounts=["6100"]))
        self.assertIn("opening_mapping_conflict", self.codes(self.claims()["revenue"]))

    def test_missing_chart_blocks_mapping_dependent_claims(self):
        (self.example / "sources/chart.json").unlink()
        self.assertTrue(all(c["status"] == "unresolved" for c in self.claims().values()))





    def test_cli_outputs_review_pack_and_exceptions_and_replays(self):
        proc = self.cli("explain", self.manifest, "--out", self.root / "out")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout); output = Path(summary["output"])
        self.assertTrue((output / "review.md").is_file())
        self.assertEqual(json.loads((output / "exceptions.json").read_text())["exceptions"], [])
        verified = self.cli("verify", output / "pack.json", "--expected-sha256", summary["pack_sha256"])
        self.assertEqual(verified.returncode, 0, verified.stderr)
        repeated = self.cli("explain", self.manifest, "--out", self.root / "out")
        self.assertEqual(json.loads(repeated.stdout)["pack_sha256"], summary["pack_sha256"])

    def test_cli_failure_retains_supported_facts_and_open_exceptions(self):
        (self.example / "sources/rent.json").unlink()
        proc = self.cli("explain", self.manifest, "--out", self.root / "out")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["supported_claims"], ["revenue"])
        self.assertEqual(summary["unresolved_claims"], ["cash"])
        exceptions = json.loads((Path(summary["output"]) / "exceptions.json").read_text())
        self.assertTrue(all(x["status"] == "open" and x["severity"] == "blocking" for x in exceptions["exceptions"]))

    def test_pin_does_not_overwrite_original_manifest(self):
        before = self.manifest.read_bytes()
        proc = self.cli("pin", self.manifest, "--output", self.manifest)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_compare_versions_uses_verified_packs(self):
        old = pack_for(self.manifest)
        self.edit_manifest(lambda m: m.update(known_at="2026-04-01"))
        earlier = pack_for(self.manifest)
        a = self.root / "earlier.json"; b = self.root / "current.json"
        a.write_text(json.dumps(earlier)); b.write_text(json.dumps(old))
        proc = self.cli("compare", a, b, "--before-sha256", fingerprint(earlier), "--after-sha256", fingerprint(old))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        changes = {c["claim_id"]: c for c in json.loads(proc.stdout)["changes"]}
        self.assertEqual(changes["cash"]["delta_cents"], -10_000)
        self.assertEqual(changes["revenue"]["delta_cents"], 0)

    def test_review_does_not_emit_source_supplied_html_or_links(self):
        self.edit_manifest(lambda m: m["claims"][0].update(label='<script>alert(1)</script> [click](https://example.invalid)'))
        review = review_markdown(evaluate_manifest(self.manifest))
        self.assertNotIn("<script>", review)
        self.assertNotIn("[click](", review)


if __name__ == "__main__":
    unittest.main()
