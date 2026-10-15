"""Real-file split/aggregate settlements, single-use coverage and retained replay."""
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
from acfr_factory.accounting_bank import bank_link_basis
from acfr_factory.accounting_evidence import capture, evaluate_manifest, pack_for, review_markdown, verify_pack
from acfr_factory.accounting_view import review_html
from acfr_factory.link_contract import fingerprint


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.example = self.root / "example"
        shutil.copytree(ROOT / "examples/accounting-evidence", self.example)
        self.manifest = self.example / "manifest.settlement.json"
        self.links = self.example / "sources/bank-settlement-links.json"
        self.support = self.example / "sources/bank-settlement-support.json"
        self.bank = self.example / "bank.settlement.export.csv"

    def repin(self, ident):
        manifest = json.loads(self.manifest.read_text())
        ref = manifest["ledger"] if ident == "ledger" else next(r for r in manifest["sources"] if r["id"] == ident)
        ref["sha256"] = hashlib.sha256((self.example / ref["path"]).read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))

    def edit(self, path, ident, action):
        doc = json.loads(path.read_text()); action(doc); path.write_text(json.dumps(doc)); self.repin(ident)

    def rebind(self, support=True):
        snapshot = capture(self.manifest)
        basis, ready = bank_link_basis(snapshot["manifest"]["claims"][0], snapshot["manifest"],
                                       {r["id"]: r for r in snapshot["files"]})
        self.assertTrue(ready)
        self.edit(self.links, "bank-links", lambda d: d.update(basis_sha256=basis))
        if support: self.edit(self.support, "settlement-support", lambda d: d.update(basis_sha256=basis))

    def bank_rows(self, action):
        reader = csv.DictReader(io.StringIO(self.bank.read_text())); fields, rows = reader.fieldnames, list(reader)
        action(rows); stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fields, lineterminator="\n"); writer.writeheader(); writer.writerows(rows)
        self.bank.write_text(stream.getvalue()); self.repin("bank-transactions")

    def cash(self):
        return evaluate_manifest(self.manifest)["claims"][0]

    def unresolved(self, code="invalid_bank_settlement"):
        report = evaluate_manifest(self.manifest)
        self.assertEqual([c["status"] for c in report["claims"]], ["unresolved", "supported"])
        cash = report["claims"][0]; self.assertIn(code, {p["code"] for p in cash["issues"]})
        return cash

    def split(self):
        self.bank_rows(lambda rows: rows.__setitem__(slice(None), [
            {"BankID": "receipt-a", "Date": "03/08/2026", "Amount": "1000.00", "Currency": "USD", "Details": "First receipt settlement"},
            {"BankID": "receipt-b", "Date": "03/09/2026", "Amount": "1500.00", "Currency": "USD", "Details": "Second receipt settlement"},
            {"BankID": "bank-rent", "Date": "03/17/2026", "Amount": "-1500.00", "Currency": "USD", "Details": "Rent"},
            {"BankID": "bank-fee", "Date": "03/31/2026", "Amount": "-100.00", "Currency": "USD", "Details": "Fee"}]))
        self.edit(self.links, "bank-links", lambda d: d.update(
            matches=[{"transaction_id": "bank-rent", "entry_id": "rent"}, {"transaction_id": "bank-fee", "entry_id": "fee"}],
            groups=[{"id": "split-receipt", "transaction_ids": ["receipt-a", "receipt-b"], "entry_ids": ["receipt"], "evidence_id": "settlement-support"}]))
        self.edit(self.support, "settlement-support", lambda d: d.update(group_id="split-receipt",
            bank_members=[{"transaction_id": "receipt-a", "amount": "1000.00"}, {"transaction_id": "receipt-b", "amount": "1500.00"}],
            ledger_members=[{"entry_id": "receipt", "amount": "2500.00"}]))
        self.rebind()

    def test_net_deposit_binds_receipt_and_fee_without_changing_ledger(self):
        cash = self.cash(); bank = cash["bank_reconciliation"]
        self.assertEqual((cash["status"], cash["ending_cents"]), ("supported", 1_090_000))
        self.assertEqual((bank["bank_activity_cents"], bank["bank_rollforward_difference_cents"]), (90_000, 0))
        group = bank["settlement_groups"][0]
        self.assertEqual((group["bank_total_cents"], group["ledger_total_cents"]), (240_000, 240_000))
        self.assertEqual([e["amount_cents"] for e in group["ledger_entries"]], [250_000, -10_000])
        self.assertEqual((len(bank["matches"]), bank["unmatched_bank_rows"], bank["unmatched_ledger_entries"]), (1, [], []))
        self.assertEqual(group["bank_rows"][0]["source_record"], 1)
        self.assertEqual(group["ledger_entries"][1]["recorded_date"], "2026-04-15")
        self.assertEqual(group["evidence_sha256"], hashlib.sha256(self.support.read_bytes()).hexdigest())

    def test_split_bank_rows_bind_one_full_book_entry(self):
        self.split(); cash = self.cash(); group = cash["bank_reconciliation"]["settlement_groups"][0]
        self.assertEqual(cash["status"], "supported")
        self.assertEqual([r["amount_cents"] for r in group["bank_rows"]], [100_000, 150_000])
        self.assertEqual(group["ledger_total_cents"], 250_000)

    def test_zero_net_internal_entry_cannot_be_smuggled_into_settlement(self):
        ledger = self.example / "ledger.export.csv"
        ledger.write_text(ledger.read_text() + 'transfer,1,1000,100.00,,USD,03/31/2026,03/31/2026,transfer,Internal\ntransfer,2,1000,,100.00,USD,03/31/2026,03/31/2026,transfer,Internal\n')
        self.repin("ledger")
        evidence = {"schema_version": "accounting-source/1", "kind": "transaction", "subject": "transfer", "unit": "USD", "amount": "100.00", "effective_date": "2026-03-31"}
        path = self.example / "sources/transfer.json"; path.write_text(json.dumps(evidence))
        manifest = json.loads(self.manifest.read_text())
        manifest["sources"].append({"id": "transfer", "path": "sources/transfer.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.manifest.write_text(json.dumps(manifest))
        self.edit(self.links, "bank-links", lambda d: d["groups"][0]["entry_ids"].append("transfer"))
        self.edit(self.support, "settlement-support", lambda d: d["ledger_members"].append({"entry_id": "transfer", "amount": "0.00"}))
        self.rebind(); self.unresolved()

    def test_full_entry_cash_net_includes_every_cash_leg_once(self):
        ledger = self.example / "ledger.export.csv"
        ledger.write_text(ledger.read_text() + 'receipt,3,1000,100.00,,USD,03/08/2026,03/08/2026,receipt,Offset cash leg\nreceipt,4,1000,,100.00,USD,03/08/2026,03/08/2026,receipt,Offset cash leg\n')
        self.repin("ledger")
        self.edit(self.example / "sources/receipt.json", "receipt", lambda d: d.update(amount="2600.00"))
        self.rebind(); cash = self.cash()
        self.assertEqual(cash["status"], "supported")
        entry = cash["bank_reconciliation"]["settlement_groups"][0]["ledger_entries"][0]
        self.assertEqual((entry["amount_cents"], len(entry["ledger_rows"])), (250_000, 3))

    def test_group_member_without_transaction_evidence_stays_unresolved(self):
        (self.example / "sources/fee.json").unlink()
        cash = self.unresolved(); self.assertEqual(cash["bank_reconciliation"]["settlement_groups"], [])

    def test_missing_or_changed_controlling_settlement_evidence_blocks_group(self):
        self.support.write_text(self.support.read_text() + "\n"); self.unresolved()
        self.support.unlink(); self.unresolved()

    def test_repinning_changed_bank_context_cannot_reuse_settlement_support(self):
        self.bank_rows(lambda rows: rows[0].update(Details="Changed business context"))
        self.rebind(support=False); self.unresolved()
        self.rebind(); self.assertEqual(self.cash()["status"], "supported")

    def test_changed_reporting_coordinate_invalidates_links_and_support(self):
        self.edit(self.manifest, "bank-links", lambda d: d.update(known_at="2026-04-17"))
        self.unresolved("invalid_bank_links")
        self.rebind(support=False); self.unresolved()

    def test_support_cannot_swap_or_offset_individual_amounts_at_equal_total(self):
        for amounts in (("2400.00", "0.00"), ("2600.00", "-200.00"), ("2500.001", "-100.001")):
            self.edit(self.support, "settlement-support", lambda d: [m.update(amount=a) for m, a in zip(d["ledger_members"], amounts)])
            self.unresolved()

    def test_exact_sum_conflict_does_not_invent_a_fee_or_allocation(self):
        self.bank_rows(lambda rows: rows[0].update(Amount="2399.99")); self.rebind()
        cash = self.unresolved(); self.assertEqual(cash["ending_cents"], 1_090_000)
        self.assertEqual(cash["bank_reconciliation"]["settlement_groups"], [])

    def test_member_reuse_between_group_and_single_link_is_refused(self):
        self.edit(self.links, "bank-links", lambda d: d["matches"].append({"transaction_id": "bank-settlement", "entry_id": "receipt"}))
        self.unresolved()

    def test_duplicate_group_id_or_evidence_or_shared_members_is_refused(self):
        original = self.links.read_text()
        for change in ({}, {"id": "another-group"}, {"id": "another-group", "evidence_id": "other-support"}):
            self.links.write_text(original)
            self.edit(self.links, "bank-links", lambda d: d["groups"].append(d["groups"][0] | change))
            self.unresolved()

    def test_unknown_duplicate_empty_or_malformed_members_fail_closed(self):
        original = self.links.read_text()
        edits = [lambda g: g.update(transaction_ids=["missing"]), lambda g: g.update(entry_ids=["receipt", "unknown"]),
                 lambda g: g.update(entry_ids=["receipt", "receipt"]), lambda g: g.update(entry_ids=[]),
                 lambda g: g.update(entry_ids="receipt"), lambda g: g.update(entry_ids=[{}, "fee"]),
                 lambda g: g.update(accepted=True), lambda g: g.update(id=" ")]
        for edit in edits:
            with self.subTest(edit=edit):
                self.links.write_text(original); self.edit(self.links, "bank-links", lambda d: edit(d["groups"][0])); self.unresolved()

    def test_settlement_evidence_has_strict_context_members_and_rationale(self):
        original = self.support.read_text()
        edits = [lambda d: d.update(subject="revenue"), lambda d: d.update(group_id="other"),
                 lambda d: d.update(unit="EUR"), lambda d: d.update(rationale=" "),
                 lambda d: d.update(approved=True), lambda d: d.update(bank_members=[]),
                 lambda d: d["ledger_members"].append(copy.deepcopy(d["ledger_members"][0])),
                 lambda d: d["bank_members"][0].update(transaction_id="another"),
                 lambda d: d["ledger_members"][0].update(leg_id="1")]
        for edit in edits:
            self.support.write_text(original); self.edit(self.support, "settlement-support", edit); self.unresolved()

    def test_general_many_to_many_and_single_pairs_need_other_contracts(self):
        for bank_ids, entry_ids in ((["bank-settlement", "bank-rent"], ["receipt", "fee"]), (["bank-settlement"], ["receipt"])):
            self.edit(self.links, "bank-links", lambda d: d["groups"][0].update(transaction_ids=bank_ids, entry_ids=entry_ids))
            self.unresolved()

    def test_split_bank_rows_with_opposite_directions_are_not_netting_groups(self):
        self.split(); self.bank_rows(lambda rows: (rows[0].update(Amount="3000.00"), rows[1].update(Amount="-500.00")))
        self.rebind(); self.unresolved()

    def test_outside_period_or_unrecorded_group_members_are_refused(self):
        self.bank_rows(lambda rows: rows[0].update(Date="04/01/2026")); self.rebind(); self.unresolved()
        self.edit(self.manifest, "bank-links", lambda d: d.update(known_at="2026-04-01"))
        self.rebind(); self.unresolved()

    def test_group_cannot_also_claim_outstanding_timing_coverage(self):
        manifest = json.loads(self.manifest.read_text()); manifest["claims"][0]["reconciliation_evidence_id"] = "timing"
        docs = {"timing": {"schema_version": "accounting-reconciliation/1", "subject": "cash", "unit": "USD", "effective_date": "2026-03-31", "items": [{"id": "pending-fee", "kind": "outstanding_payment", "entry_id": "fee", "amount": "100.00", "evidence_id": "pending-fee"}]},
                "pending-fee": {"schema_version": "accounting-source/1", "kind": "timing_item", "subject": "pending-fee", "unit": "USD", "amount": "100.00", "effective_date": "2026-03-31", "entry_id": "fee", "timing_kind": "outstanding_payment"}}
        for ident, doc in docs.items():
            path = self.example / "sources" / (ident + ".json"); path.write_text(json.dumps(doc))
            manifest["sources"].append({"id": ident, "path": "sources/" + path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.manifest.write_text(json.dumps(manifest)); self.unresolved()

    def test_version_one_remains_strict_and_group_schema_requires_groups(self):
        original = self.links.read_text()
        for edit in (lambda d: d.update(schema_version="accounting-bank-links/1"), lambda d: d.pop("groups"), lambda d: d.update(groups=None)):
            self.links.write_text(original); self.edit(self.links, "bank-links", edit); self.unresolved("invalid_bank_links")

    def test_empty_group_list_never_automatically_discovers_settlements(self):
        self.edit(self.links, "bank-links", lambda d: d.update(groups=[]))
        cash = self.unresolved("unmatched_bank_transaction")
        self.assertEqual(cash["bank_reconciliation"]["settlement_groups"], [])
        self.assertEqual(cash["bank_reconciliation"]["unmatched_bank_rows"][0]["candidate_ledger_entries"], [])

    def test_offline_pack_replay_and_forged_group_facts(self):
        pack = pack_for(self.manifest); digest = fingerprint(pack); shutil.rmtree(self.example)
        self.assertEqual(verify_pack(pack, digest)["status"], "pass")
        pack["report"]["claims"][0]["bank_reconciliation"]["settlement_groups"][0]["ledger_total_cents"] += 1
        with self.assertRaises(ValueError): verify_pack(pack, fingerprint(pack))

    def test_actual_cli_and_readable_review_expose_members_and_matched_counts(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/accounting_evidence.py"), "explain", str(self.manifest), "--out", str(self.root / "packs")],capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        pack = pack_for(self.manifest); page = Path(json.loads(result.stdout)["review_html"]).read_text()
        self.assertIn('Matched bank rows</span><strong>2</strong>', page)
        self.assertIn("Settlement net-receipt", page); self.assertIn("receipt: $2,500.00; fee: −$100.00", page)
        self.assertIn("Inspect settlement-support", page)
        self.assertIn("Full ledger member fee: -100.00 USD", review_markdown(pack["report"]))



if __name__ == "__main__": unittest.main()
