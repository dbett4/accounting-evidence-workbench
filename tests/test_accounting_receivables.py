"""Retained invoice populations, explicit allocations and actual readiness consumers."""
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
from acfr_factory.accounting_evidence import capture, pack_for, evaluate_manifest, verify_pack
from acfr_factory.accounting_receivables import receivables_basis
from acfr_factory.link_contract import fingerprint


class ReceivablesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.example = self.root / "example"
        shutil.copytree(ROOT / "examples/accounting-evidence/receivables", self.example)
        self.manifest = self.example / "manifest.json"
        self.schedule = self.example / "sources/ar-schedule.json"
        self.opening = self.example / "opening.export.csv"
        self.ending = self.example / "ending.export.csv"
        self.mapping = self.example / "population.mapping.json"

    def pin(self):
        doc = json.loads(self.manifest.read_text())
        refs = [doc["ledger"], *doc["sources"]]
        for ref in refs:
            p = self.example / ref["path"]
            if p.exists(): ref["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(doc))

    def edit(self, path, action, pin=True):
        doc = json.loads(path.read_text()); action(doc); path.write_text(json.dumps(doc))
        if pin: self.pin()

    def rows(self, path, action):
        reader = csv.DictReader(io.StringIO(path.read_text())); fields, rows = reader.fieldnames, list(reader)
        action(rows); out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fields, lineterminator="\n"); writer.writeheader(); writer.writerows(rows)
        path.write_text(out.getvalue()); self.pin()

    def rebind(self, evidence=True):
        snapshot = capture(self.manifest); definition = json.loads(self.schedule.read_text())
        basis, ready = receivables_basis(snapshot["manifest"]["claims"][0], snapshot["manifest"],
                                        {r["id"]: r for r in snapshot["files"]}, definition)
        self.assertTrue(ready)
        self.edit(self.schedule, lambda d: d.update(basis_sha256=basis))
        if evidence:
            for path in self.example.glob('sources/*.json'):
                doc = json.loads(path.read_text())
                if doc.get("schema_version") == "accounting-receivable-movement/1":
                    self.edit(path, lambda d: d.update(basis_sha256=basis))

    def claims(self): return evaluate_manifest(self.manifest)["claims"]
    def ar(self): return self.claims()[0]["receivables_reconciliation"]

    def unresolved(self, code, all_claims=False):
        claims = self.claims(); self.assertEqual(claims[0]["status"], "unresolved")
        if not all_claims: self.assertEqual([c["status"] for c in claims[1:]], ["supported", "supported"])
        self.assertIn(code, {p["code"] for p in claims[0]["issues"]}); return claims[0]["receivables_reconciliation"]

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/accounting_evidence.py'), *map(str, args)],capture_output=True,text=True)

    def test_authored_invoice_and_ledger_bridges(self):
        claims = self.claims(); ar = claims[0]["receivables_reconciliation"]
        self.assertEqual([c["status"] for c in claims], ["supported"] * 3)
        self.assertEqual((claims[0]["opening_cents"], claims[0]["activity_cents"], claims[0]["ending_cents"]), (200_000, -40_000, 160_000))
        self.assertEqual((claims[1]["ending_cents"], claims[2]["ending_cents"]), (1_190_000, 150_000))
        self.assertEqual((ar["opening_population_cents"], ar["allocated_activity_cents"], ar["ending_population_cents"]), (200_000, -40_000, 160_000))
        self.assertEqual((ar["opening_minus_ledger_cents"], ar["ending_minus_ledger_cents"]), (0, 0))
        self.assertEqual(ar["retained_movement_count"], 4)

    def test_fully_collected_invoice_can_be_absent_without_dropping_a_residual(self):
        invoice = self.ar()["invoices"][0]
        self.assertEqual((invoice["invoice_id"], invoice["opening_cents"], invoice["movement_cents"]), ("INV-100", 120_000, -120_000))
        self.assertEqual((invoice["reported_ending_cents"], invoice["ending_presence"], invoice["status"]), (0, "settled_and_absent", "supported"))

    def test_positive_missing_invoice_remains_unknown_even_when_other_balances_offset(self):
        self.rows(self.ending, lambda rows: rows.__setitem__(slice(None), [{**rows[0], "Outstanding": "1600.00"}]))
        self.rebind(); ar = self.unresolved("missing_ending_receivable")
        new = next(r for r in ar["invoices"] if r["invoice_id"] == "INV-102")
        self.assertIsNone(new["reported_ending_cents"])
        self.assertEqual(ar["ending_minus_ledger_cents"], 0)

    def test_equal_aggregate_cannot_mask_offsetting_individual_balance_errors(self):
        self.rows(self.ending, lambda rows: (rows[0].update(Outstanding="700.00"), rows[1].update(Outstanding="900.00")))
        self.rebind(); ar = self.unresolved("receivable_invoice_difference")
        self.assertEqual(ar["ending_minus_ledger_cents"], 0)
        self.assertEqual([r["difference_cents"] for r in ar["invoices"]], [0, -10_000, 10_000])

    def test_party_issue_or_due_date_drift_is_an_identity_conflict(self):
        original = self.ending.read_text()
        for change in ({"Customer": "Another party"}, {"Issued": "02/11/2026"}, {"Due": "03/11/2026"}):
            self.ending.write_text(original); self.rows(self.ending, lambda rows: rows[0].update(change)); self.rebind()
            self.unresolved("receivable_identity_conflict")

    def test_unknown_ending_invoice_is_not_an_opening_plug(self):
        self.rows(self.ending, lambda rows: rows[0].update(Invoice="unknown")); self.rebind()
        self.unresolved("unknown_ending_receivable")

    def test_populations_require_unique_ids_valid_exact_amounts_currency_and_dates(self):
        original = self.opening.read_text()
        edits = [lambda rows: rows[1].update(Invoice="INV-100"), lambda rows: rows[0].update(Outstanding="1200.001"),
                 lambda rows: rows[0].update(Outstanding="1.2e3"), lambda rows: rows[0].update(Outstanding=""),
                 lambda rows: rows[0].update(Outstanding="-1200.00"), lambda rows: rows[0].update(Currency="EUR"),
                 lambda rows: rows[0].update(Issued="04/01/2026"), lambda rows: rows[0].update(Due="12/31/2025"),
                 lambda rows: rows[0].update(Customer=" ")]
        for edit in edits:
            self.opening.write_text(original); self.rows(self.opening, edit); self.rebind()
            ar = self.unresolved("invalid_receivables_population"); self.assertIsNone(ar["opening_population_cents"])

    def test_truncated_csv_or_unreviewed_extra_columns_cannot_silently_drop_rows(self):
        original = self.ending.read_text()
        for suffix in ('"unfinished', 'a,b,c\n', 'a,b,c,d,e,f,g,h\n'):
            self.ending.write_text(original + suffix); self.pin(); self.rebind()
            self.unresolved("invalid_receivables_population")

    def test_unpinned_or_missing_population_only_blocks_its_dependent_claim(self):
        self.ending.write_text(self.ending.read_text() + "\n"); self.unresolved("invalid_receivables_population")
        self.ending.unlink(); self.unresolved("invalid_receivables_population")

    def test_opening_population_must_tie_the_explicit_ledger_baseline(self):
        self.rows(self.opening, lambda rows: rows[0].update(Outstanding="1100.00")); self.rebind()
        ar = self.unresolved("receivables_population_difference")
        self.assertEqual(ar["opening_minus_ledger_cents"], -10_000)

    def test_ending_population_must_tie_ledger_even_with_supported_allocations(self):
        self.rows(self.ending, lambda rows: rows[1].update(Outstanding="799.99")); self.rebind()
        ar = self.unresolved("receivables_population_difference")
        self.assertEqual(ar["ending_minus_ledger_cents"], -1)

    def test_batch_entry_allocations_sum_exactly_and_count_all_cash_or_ar_legs(self):
        batch = next(r for r in self.ar()["entry_coverage"] if r["entry_id"] == "collect-batch")
        self.assertEqual((batch["amount_cents"], batch["allocated_cents"], batch["difference_cents"]), (-100_000, -100_000, 0))
        self.assertEqual(batch["movement_ids"], ["batch-old", "batch-new"])

    def test_missing_allocation_is_not_guessed_from_equal_totals(self):
        self.edit(self.schedule, lambda d: d["movements"].pop())
        self.unresolved("receivable_entry_coverage")

    def test_wrong_individual_evidence_amounts_cannot_offset_to_a_valid_batch_total(self):
        self.edit(self.example/'sources/batch-old.json', lambda d: d.update(amount="-400.00"))
        self.edit(self.example/'sources/batch-new.json', lambda d: d.update(amount="-600.00"))
        self.unresolved("invalid_receivable_movement")

    def test_missing_or_changed_movement_support_preserves_the_unresolved_allocation(self):
        p=self.example/'sources/batch-old.json'; p.write_text(p.read_text()+"\n")
        ar=self.unresolved("invalid_receivable_movement")
        self.assertEqual(next(r for r in ar["movements"] if r["id"]=="batch-old")["amount_cents"], -30_000)
        p.unlink(); self.unresolved("invalid_receivable_movement")

    def test_repinning_population_context_requires_new_schedule_and_movement_basis(self):
        self.rows(self.ending, lambda rows: rows[0].update(Description="Changed retained context"))
        self.unresolved("stale_receivables_binding")
        self.rebind(evidence=False); self.unresolved("invalid_receivable_movement")
        self.rebind(); self.assertEqual(self.claims()[0]["status"], "supported")

    def test_changed_mapping_and_reporting_coordinates_invalidate_bindings(self):
        self.mapping.write_text(self.mapping.read_text()+"\n"); self.pin(); self.unresolved("stale_receivables_binding")
        self.rebind(); self.assertEqual(self.ar()["status"], "reconciled")
        self.edit(self.manifest, lambda d: d.update(known_at="2026-04-01"))
        self.unresolved("invalid_receivables_schedule")
        self.edit(self.schedule, lambda d: d.update(known_at="2026-04-01"))
        self.unresolved("stale_receivables_binding")

    def test_allocation_kind_sign_and_precision_are_explicit(self):
        original=self.schedule.read_text()
        for change in ({"kind":"writeoff"},{"kind":"credit_note"},{"amount":"-1500.00"},{"amount":"1500.001"},{"amount":"1.5e3"},{"amount":"0.00"}):
            self.schedule.write_text(original); self.edit(self.schedule, lambda d:d["movements"][0].update(change))
            self.unresolved("invalid_receivable_movement")

    def test_unknown_duplicate_extra_or_malformed_movement_definitions_fail_closed(self):
        original=self.schedule.read_text()
        edits=[lambda d:d["movements"][0].update(entry_id="unknown"),lambda d:d["movements"].append(copy.deepcopy(d["movements"][0])),
               lambda d:d["movements"][1].update(evidence_id="new-invoice"),lambda d:d["movements"][0].update(accepted=True),
               lambda d:d["movements"].append(None),lambda d:d["movements"][0].update(kind={})]
        for edit in edits:
            self.schedule.write_text(original);self.edit(self.schedule,edit);self.unresolved("invalid_receivable_movement")

    def test_movement_evidence_cannot_change_identity_coordinates_or_claim_approval(self):
        p=self.example/'sources/old-payment.json';original=p.read_text()
        for change in ({"customer_id":"B"},{"invoice_id":"INV-101"},{"entry_id":"issue-new"},{"recorded_date":"2026-03-21"},{"unit":"EUR"},{"approved":True},{"rationale":" "}):
            p.write_text(original);self.edit(p,lambda d:d.update(change))
            code="receivable_identity_conflict" if change=={"customer_id":"B"} else "invalid_receivable_movement"
            self.unresolved(code)

    def test_new_invoice_cannot_be_reissued_under_an_opening_id(self):
        self.edit(self.schedule,lambda d:d["movements"][0].update(invoice_id="INV-100"))
        self.edit(self.example/'sources/new-invoice.json',lambda d:d.update(invoice_id="INV-100",customer_id="A",issue_date="2026-01-01",due_date="2026-01-31"))
        self.unresolved("invalid_receivable_invoice")

    def test_overcollection_is_detected_even_when_total_allocations_and_ending_tie(self):
        self.edit(self.schedule,lambda d:(d["movements"][2].update(amount="-400.00"),d["movements"][3].update(amount="-600.00")))
        self.edit(self.example/'sources/batch-old.json',lambda d:d.update(amount="-400.00"))
        self.edit(self.example/'sources/batch-new.json',lambda d:d.update(amount="-600.00"))
        ar=self.unresolved("receivable_overcollection")
        self.assertEqual((ar["allocated_activity_cents"],ar["ending_minus_ledger_cents"]),(-40_000,0))
        self.assertEqual(ar["invoices"][0]["calculated_ending_cents"],-10_000)

    def test_collection_cannot_be_netted_into_a_positive_invoice_entry(self):
        self.edit(self.schedule,lambda d:d["movements"][1].update(invoice_id="INV-102",entry_id="issue-new",amount="-900.00"))
        self.edit(self.example/'sources/old-payment.json',lambda d:d.update(invoice_id="INV-102",entry_id="issue-new",customer_id="A",issue_date="2026-03-12",due_date="2026-04-11",effective_date="2026-03-12",recorded_date="2026-03-12"))
        # A collection mixed into the positive invoice entry is unsupported even at the same day.
        self.unresolved("receivable_entry_coverage")

    def test_collection_before_invoice_issue_is_refused_even_with_exact_entry_amount(self):
        self.rows(self.example/'ledger.csv', lambda rows: [row.update(effective_date='2026-03-05',recorded_date='2026-03-05') for row in rows if row['entry_id']=='collect-old'])
        self.edit(self.example/'sources/collect-old.json',lambda d:d.update(effective_date='2026-03-05'))
        self.edit(self.schedule,lambda d:d['movements'][1].update(invoice_id='INV-102'))
        self.edit(self.example/'sources/old-payment.json',lambda d:d.update(invoice_id='INV-102',customer_id='A',issue_date='2026-03-12',due_date='2026-04-11',effective_date='2026-03-05',recorded_date='2026-03-05'))
        self.rebind();self.unresolved('receivable_identity_conflict')

    def test_compact_ledger_date_cannot_hide_a_collection_before_invoice_issue(self):
        self.rows(self.example/'ledger.csv',lambda rows:[row.update(effective_date='20260305',recorded_date='20260305') for row in rows if row['entry_id']=='collect-old'])
        self.edit(self.example/'sources/collect-old.json',lambda d:d.update(effective_date='2026-03-05'))
        self.edit(self.schedule,lambda d:(d['movements'][1].update(invoice_id='INV-102'),d['movements'][2].update(amount='-1000.00'),d['movements'].pop()))
        self.edit(self.example/'sources/old-payment.json',lambda d:d.update(invoice_id='INV-102',customer_id='A',issue_date='2026-03-12',due_date='2026-04-11',effective_date='20260305',recorded_date='20260305'))
        self.edit(self.example/'sources/batch-old.json',lambda d:d.update(amount='-1000.00'))
        self.rows(self.ending,lambda rows:(rows[1].update(Outstanding='600.00'),rows.append({'Invoice':'INV-100','Customer':'A','Issued':'01/01/2026','Due':'01/31/2026','Outstanding':'200.00','Currency':'USD','Description':'Retained positive balance'})))
        self.rebind();self.unresolved('receivable_identity_conflict')

    def test_every_selected_receivable_leg_contributes_to_the_full_entry_net(self):
        p=self.example/'ledger.csv'
        p.write_text(p.read_text()+'collect-batch,3,1200,100.00,0.00,USD,2026-03-25,2026-03-25,collect-batch\ncollect-batch,4,1200,0.00,100.00,USD,2026-03-25,2026-03-25,collect-batch\n')
        self.edit(self.example/'sources/collect-batch.json',lambda d:d.update(amount='1100.00'));self.rebind()
        ar=self.ar();self.assertEqual(ar['status'],'reconciled')
        entry=next(r for r in ar['entry_coverage'] if r['entry_id']=='collect-batch')
        self.assertEqual((entry['amount_cents'],len(entry['ledger_rows'])),(-100_000,3))

    def test_explicit_zero_ending_row_is_preserved_as_a_retained_record(self):
        self.rows(self.ending,lambda rows:rows.append({'Invoice':'INV-100','Customer':'A','Issued':'01/01/2026','Due':'01/31/2026','Outstanding':'0.00','Currency':'USD','Description':'Explicit paid invoice'}))
        self.rebind();invoice=self.ar()['invoices'][0]
        self.assertEqual((invoice['status'],invoice['reported_ending_cents'],invoice['ending_presence']),('supported',0,'retained'))

    def test_earlier_and_later_cutoffs_preserve_their_own_population_and_ledger_views(self):
        later=pack_for(self.manifest);later_digest=fingerprint(later)
        self.edit(self.manifest,lambda d:d.update(period_end='2026-03-22',known_at='2026-03-22'))
        self.edit(self.schedule,lambda d:(d.update(period_end='2026-03-22',known_at='2026-03-22'),d.update(movements=d['movements'][:2])))
        self.rows(self.ending,lambda rows:(rows[1].update(Outstanding='1500.00'),rows.append({'Invoice':'INV-100','Customer':'A','Issued':'01/01/2026','Due':'01/31/2026','Outstanding':'300.00','Currency':'USD','Description':'Earlier retained population'})))
        self.rebind();earlier=pack_for(self.manifest);earlier_digest=fingerprint(earlier)
        self.assertEqual((earlier['report']['status'],earlier['report']['claims'][0]['ending_cents']),('pass',260_000))
        self.assertEqual(verify_pack(later,later_digest)['claims'][0]['ending_cents'],160_000)
        a=self.root/'earlier.json';b=self.root/'later.json';a.write_text(json.dumps(earlier));b.write_text(json.dumps(later))
        r=self.cli('compare',a,b,'--before-sha256',earlier_digest,'--after-sha256',later_digest)
        self.assertEqual(r.returncode,0,r.stdout);result=json.loads(r.stdout)
        change=next(row for row in result['changes'] if row['claim_id']=='receivables')
        self.assertEqual(change['delta_cents'],-100_000)
        self.assertEqual((result['before_coordinate']['period_end'],result['after_coordinate']['period_end']),('2026-03-22','2026-03-31'))

    def test_unknown_invoice_allocation_is_not_a_population_inference(self):
        self.edit(self.schedule,lambda d:d["movements"][1].update(invoice_id="unknown"))
        self.edit(self.example/'sources/old-payment.json',lambda d:d.update(invoice_id="unknown"))
        self.unresolved("unknown_receivable_invoice")

    def test_file_order_does_not_invent_intraday_movement_order(self):
        self.edit(self.schedule,lambda d:d["movements"].reverse())
        self.assertEqual(self.claims()[0]["status"],"supported")
        self.assertEqual([r["reported_ending_cents"] for r in self.ar()["invoices"]],[0,80_000,80_000])

    def test_population_row_lineage_mapping_and_simple_aging_are_retained(self):
        ar=self.ar();old=ar["invoices"][1]
        self.assertEqual((old["opening_record"]["source_record"],old["ending_record"]["source_record"]),(2,1))
        self.assertEqual((old["opening_record"]["source_line_start"],old["ending_record"]["source_line_end"]),(3,2))
        self.assertEqual(ar["aging_cents"],{"current":80_000,"1_30":80_000,"31_60":0,"61_90":0,"91_plus":0})
        self.assertEqual(old["overdue_days"],21)
        self.assertEqual(ar["normalization"]["ending"]["rules"]["ignored_columns"],["Description"])

    def test_original_utf8_bom_and_crlf_bytes_survive_retention(self):
        raw=b'\xef\xbb\xbf'+self.ending.read_bytes().replace(b'\n',b'\r\n');self.ending.write_bytes(raw);self.pin();self.rebind()
        pack=pack_for(self.manifest);record=next(r for r in pack["snapshot"]["files"] if r["id"]=="ar-ending")
        self.assertEqual(record["content"].encode(),raw);self.assertEqual(pack["report"]["status"],"pass")

    def test_missing_transaction_or_invalid_ledger_basis_cannot_support_allocations(self):
        (self.example/'sources/collect-batch.json').unlink()
        self.unresolved("receivable_entry_coverage",all_claims=True)
        ledger=self.example/'ledger.csv';ledger.write_text(ledger.read_text()+"\n")
        ar=self.unresolved("unsupported_receivables_basis",all_claims=True);self.assertFalse(ar["binding_basis_ready"])

    def test_normalized_populations_work_without_mapping_definitions(self):
        from acfr_factory.accounting_source_mapping import normalize_receivables_export
        mapping=json.loads(self.mapping.read_text())
        for path in (self.opening,self.ending):path.write_text(normalize_receivables_export(path.read_text(),mapping)[0])
        self.edit(self.schedule,lambda d:(d.pop("opening_mapping_id"),d.pop("ending_mapping_id")))
        self.rebind();self.assertEqual(self.ar()["status"],"reconciled")

    def test_unknown_mapping_rules_or_blank_zero_assertions_are_refused(self):
        original=self.mapping.read_text()
        for change in ({"blank_as_zero":True},{"format":"guess"},{"constant":"800.00"}):
            self.mapping.write_text(original);self.edit(self.mapping,lambda d:d["columns"]["amount"].update(change));self.rebind()
            self.unresolved("invalid_receivables_population")

    def test_wrong_schedule_context_schema_extra_fields_or_missing_reference_fail(self):
        original=self.schedule.read_text()
        for change in ({"subject":"cash"},{"scope":"another entity"},{"period_start":"2026-03-02"},{"approved":True},{"movements":None}):
            self.schedule.write_text(original);self.edit(self.schedule,lambda d:d.update(change));self.unresolved("invalid_receivables_schedule")
        self.schedule.write_text(original);self.edit(self.schedule,lambda d:d.update(opening_population_id="missing"));self.unresolved("invalid_receivables_population")

    def test_actual_template_drafts_empty_allocations_and_refuses_bad_basis_or_overwrite(self):
        out=self.root/'new-template.json';r=self.cli('receivables-template',self.manifest,'--claim','receivables','--output',out)
        self.assertEqual(r.returncode,0,r.stdout);result=json.loads(r.stdout)
        self.assertEqual(json.loads(out.read_text())["movements"],[])
        self.assertEqual(result["authority"],"draft_only_no_automatic_allocations")
        self.assertEqual(len(result["visible_ledger_entries"]),3)
        out.write_text('Earlier retained template')
        self.assertEqual(self.cli('receivables-template',self.manifest,'--claim','receivables','--output',out).returncode,2)
        self.ending.unlink()
        self.assertEqual(self.cli('receivables-template',self.manifest,'--claim','receivables','--output',self.root/'bad.json').returncode,2)

    def test_offline_replay_recomputes_invoice_facts_and_refuses_forged_saved_pass(self):
        self.ending.unlink();pack=pack_for(self.manifest);digest=fingerprint(pack);shutil.rmtree(self.example)
        self.assertEqual(verify_pack(pack,digest)["status"],"fail")
        pack["report"]["claims"][0]["status"]="supported"
        with self.assertRaises(ValueError):verify_pack(pack,fingerprint(pack))

    def test_actual_cli_page_and_markdown_show_exact_invoice_members_and_sources(self):
        r=self.cli('explain',self.manifest,'--out',self.root/'packs');self.assertEqual(r.returncode,0,r.stdout)
        result=json.loads(r.stdout);page=Path(result['review_html']).read_text();md=(Path(result['output'])/'review.md').read_text()
        self.assertIn('Invoice receivables support',page);self.assertIn('Inspect ar-opening',page)
        self.assertIn('Settled; absent from ending export',page);self.assertIn('Full entry collect-batch',page)
        self.assertIn('Reported population aging',page);self.assertIn('Invoice INV-101',md)
        self.assertEqual(self.cli('explain',self.manifest,'--out',self.root/'packs').stdout,r.stdout)
        regenerated=self.cli('view',Path(result['output'])/'pack.json','--expected-sha256',result['pack_sha256'],'--output',result['review_html'])
        self.assertEqual(regenerated.returncode,0,regenerated.stdout)
        self.assertEqual(json.loads(regenerated.stdout)['html_sha256'],result['html_sha256'])

    def test_missing_schedule_still_emits_a_reviewable_unresolved_page(self):
        self.schedule.unlink();r=self.cli('explain',self.manifest,'--out',self.root/'packs')
        self.assertEqual(r.returncode,1,r.stdout);result=json.loads(r.stdout)
        page=Path(result['review_html']).read_text()
        self.assertIn('Provisional ending amount',page);self.assertIn('of unavailable retained movement definitions',page)
        self.assertNotIn('of None',page);self.assertEqual(result['supported_claims'],['cash','revenue'])



if __name__ == '__main__':unittest.main()
