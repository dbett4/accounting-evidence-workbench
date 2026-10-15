"""Retained raw exports and reviewer annotations exercised through real files/CLI."""
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
from acfr_factory.accounting_evidence import evaluate_manifest, pack_for, verify_pack, review_markdown
from acfr_factory.accounting_source_mapping import normalize_export
from acfr_factory.accounting_review import notes_template, review_notes, notes_markdown
from acfr_factory.link_contract import fingerprint


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.example = self.root / "example"
        shutil.copytree(ROOT / "examples/accounting-evidence", self.example)
        self.manifest = self.example / "manifest.mapped.json"
        self.mapping = self.example / "ledger.mapping.json"
        self.export = self.example / "ledger.export.csv"

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/accounting_evidence.py"), *map(str, args)],
                              text=True, capture_output=True, check=False)

    def repin(self):
        manifest = json.loads(self.manifest.read_text())
        for ref in [manifest["ledger"], manifest["ledger"]["mapping"]]:
            ref["sha256"] = hashlib.sha256((self.example / ref["path"]).read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))

    def alter_mapping(self, edit):
        mapping = json.loads(self.mapping.read_text())
        edit(mapping)
        self.mapping.write_text(json.dumps(mapping))
        self.repin()

    def alter_export(self, edit):
        reader = csv.DictReader(io.StringIO(self.export.read_text()))
        fields, rows = reader.fieldnames, list(reader)
        edit(rows)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
        self.export.write_text(output.getvalue())
        self.repin()

    def report(self):
        return evaluate_manifest(self.manifest)

    def assert_global_failure(self, code):
        report = self.report()
        self.assertEqual(report["status"], "fail")
        for claim in report["claims"]:
            self.assertEqual(claim["numerical_basis"], "provisional")
            self.assertIn(code, {item["code"] for item in claim["issues"]})

    def unresolved_pack(self):
        (self.example / "sources/rent.json").unlink()
        pack = pack_for(self.manifest)
        return pack, fingerprint(pack)

    def filled_notes(self, report, digest):
        notes = notes_template(report, digest)
        for note in notes["notes"]:
            note.update(classification="source_gap", owner="preparer",
                        rationale="The controlling rent evidence is missing.",
                        next_action="Obtain the rent source and re-run intake against a reviewed version.")
        return notes

    def test_export_agrees_with_authored_balances_and_retains_raw_bytes(self):
        pack = pack_for(self.manifest)
        report = pack["report"]
        self.assertEqual(report["status"], "pass")
        self.assertEqual([c["ending_cents"] for c in report["claims"]], [1_090_000, 250_000])
        self.assertEqual(report["normalization"]["source_records"], 6)
        self.assertEqual(report["normalization"]["normalized_ledger_sha256"],
                         hashlib.sha256((self.example / "ledger.csv").read_bytes()).hexdigest())
        files = {r["id"]: r for r in pack["snapshot"]["files"]}
        self.assertEqual(files["ledger"]["content"], self.export.read_text())
        self.assertEqual(files["ledger-mapping"]["content"], self.mapping.read_text())
        leg = next(x for x in report["claims"][0]["trace"] if x["entry_id"] == "rent")
        self.assertEqual((leg["source_record"], leg["source_line_start"], leg["source_line_end"]), (4, 5, 6))
        self.assertEqual(leg["ledger_revision"], files["ledger"]["actual_sha256"])
        self.assertEqual(leg["mapping_revision"], files["ledger-mapping"]["actual_sha256"])
        self.assertIn("physical lines 5–6", review_markdown(report))

    def test_original_and_mapping_deletion_do_not_break_retained_replay(self):
        pack = pack_for(self.manifest); digest = fingerprint(pack)
        shutil.rmtree(self.example)
        self.assertEqual(verify_pack(pack, digest), pack["report"])

    def test_raw_export_or_mapping_revision_changes_block_all_dependents(self):
        for path in (self.export, self.mapping):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assert_global_failure("source_revision_changed")
                path.write_bytes(original)

    def test_missing_mapping_blocks_all_claims(self):
        self.mapping.unlink()
        self.assert_global_failure("missing_or_unreadable_source")

    def test_unmapped_extra_column_or_missing_source_column_cannot_disappear(self):
        for kind in ("extra", "missing"):
            with self.subTest(kind=kind):
                original = self.export.read_text()
                modified = original.replace("Description", "Unmapped") if kind == "extra" else original.replace("EnteredDate", "Unknown")
                self.export.write_text(modified); self.repin()
                self.assert_global_failure("invalid_export_normalization")
                self.export.write_text(original); self.repin()

    def test_duplicate_headers_and_malformed_csv_fail_closed(self):
        original = self.export.read_text()
        for modified in (original.replace("Description", "Document"), original + '"unfinished',
                         original + "too,few,columns\n", original + "a,b,c,d,e,f,g,h,i,j,extra\n"):
            with self.subTest(modified=modified[-30:]):
                self.export.write_text(modified); self.repin()
                self.assert_global_failure("invalid_export_normalization")
        self.export.write_text(original); self.repin()

    def test_recorded_date_cannot_be_inferred_or_supplied_as_constant(self):
        self.alter_mapping(lambda m: m["columns"].update(recorded_date={"constant": "2026-04-16"}))
        self.assert_global_failure("invalid_export_normalization")

    def test_unknown_mapping_rules_and_partial_or_ignored_mappings_fail(self):
        original = self.mapping.read_text()
        edits = [lambda m: m.update(guess_accounts=True),
                 lambda m: m["columns"]["debit"].update(blank_as_zerro=True),
                 lambda m: m["columns"].pop("account"),
                 lambda m: m["ignored_columns"].append("Account"),
                 lambda m: m["columns"]["debit"].update(blank_as_zero="true")]
        for edit in edits:
            with self.subTest(edit=edit):
                self.mapping.write_text(original); self.alter_mapping(edit)
                self.assert_global_failure("invalid_export_normalization")

    def test_blanks_require_explicit_zero_policy_and_nonzero_invalid_amounts_fail(self):
        self.alter_mapping(lambda m: m["columns"]["debit"].pop("blank_as_zero"))
        self.assert_global_failure("invalid_export_normalization")

    def test_no_rounding_exponents_or_malformed_grouping(self):
        original = self.export.read_text()
        for amount in ("25,00.00", "2500.001", "2.5e3", " 2500.00", "(2500.00)"):
            with self.subTest(amount=amount):
                self.export.write_text(original)
                self.alter_export(lambda rows: rows[0].update(Debit=amount))
                self.assert_global_failure("invalid_export_normalization")

    def test_blank_recording_date_and_ambiguous_unspecified_dates_are_not_guessed(self):
        self.alter_export(lambda rows: rows[0].update(EnteredDate=""))
        self.assert_global_failure("invalid_export_normalization")

    def test_explicit_date_order_changes_coordinate_and_requires_matching_evidence(self):
        self.alter_mapping(lambda m: m["columns"]["effective_date"].update(format="dmy_slash"))
        self.assert_global_failure("invalid_export_normalization")  # March 31 cannot be a month.

    def test_explicit_iso_and_constant_unit_and_shared_evidence_column(self):
        content = (self.example / "ledger.csv").read_text()
        mapping = {"schema_version": "accounting-column-map/1",
                   "columns": {k: {"column": k} for k in csv.DictReader(io.StringIO(content)).fieldnames},
                   "ignored_columns": []}
        normalized, lineage = normalize_export(content, mapping)
        self.assertEqual(normalized, content)
        self.assertEqual(lineage[-1]["source_line_end"], 7)
        mapping["columns"]["unit"] = {"constant": "USD"}; mapping["ignored_columns"].append("unit")
        mapping["columns"]["evidence_id"] = {"column": "entry_id"}; mapping["ignored_columns"].append("evidence_id")
        self.assertEqual(normalize_export(content, mapping)[0], content)

    def test_missing_evidence_still_only_blocks_its_claim_after_normalization(self):
        pack, digest = self.unresolved_pack()
        self.assertEqual([c["status"] for c in pack["report"]["claims"]], ["unresolved", "supported"])
        self.assertEqual(verify_pack(pack, digest)["status"], "fail")

    def test_mapping_change_requires_a_new_pack_even_with_same_balances(self):
        before = pack_for(self.manifest)
        # Replace the source-unit mapping with an explicit USD assertion.
        self.alter_mapping(lambda m: (m["columns"].update(unit={"constant": "USD"}), m["ignored_columns"].append("Currency")))
        after = pack_for(self.manifest)
        self.assertNotEqual(fingerprint(before), fingerprint(after))
        self.assertEqual(after["report"]["status"], "pass")
        self.assertEqual([c["ending_cents"] for c in before["report"]["claims"]],
                         [c["ending_cents"] for c in after["report"]["claims"]])

    def test_forged_normalization_or_lineage_is_refused_by_recomputation(self):
        for edit in (lambda p: p["report"]["normalization"].update(source_records=5),
                     lambda p: p["report"]["claims"][0]["trace"][0].update(source_line_start=999)):
            pack = pack_for(self.manifest); edit(pack)
            with self.assertRaisesRegex(ValueError, "recomputation"):
                verify_pack(pack, fingerprint(pack))

    def test_original_utf8_bom_is_retained_and_mapping_bom_is_supported(self):
        for path in (self.export, self.mapping): path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.repin()
        pack = pack_for(self.manifest)
        self.assertEqual(pack["report"]["status"], "pass")
        self.assertTrue(pack["snapshot"]["files"][0]["content"].startswith("\ufeff"))

    def test_crlf_export_bytes_and_multiline_lineage_are_preserved(self):
        raw = self.export.read_bytes().replace(b"\n", b"\r\n")
        self.export.write_bytes(raw); self.repin()
        pack = pack_for(self.manifest)
        self.assertEqual(pack["report"]["status"], "pass")
        self.assertEqual(pack["snapshot"]["files"][0]["content"].encode("utf-8"), raw)
        rent = next(x for x in pack["report"]["claims"][0]["trace"] if x["entry_id"] == "rent")
        self.assertEqual((rent["source_line_start"], rent["source_line_end"]), (5, 6))

    def test_cli_pin_rebases_nested_mapping_reference_and_preserves_versions(self):
        destination = self.root / "new/version.json"
        self.assertEqual(self.cli("pin", self.manifest, "--output", destination).returncode, 0)
        self.assertEqual(evaluate_manifest(destination)["status"], "pass")
        ref = json.loads(destination.read_text())["ledger"]["mapping"]
        self.assertEqual((destination.parent / ref["path"]).resolve(), self.mapping.resolve())
        self.assertEqual(self.cli("explain", destination, "--out", self.root / "packs").returncode, 0)


    def test_reviewer_notes_do_not_change_amounts_or_open_exceptions(self):
        pack, digest = self.unresolved_pack(); report = pack["report"]
        reviewed = review_notes(report, digest, self.filled_notes(report, digest))
        self.assertEqual(reviewed["support_status"], "fail")
        self.assertEqual(reviewed["exception_register"], report["exception_register"])
        self.assertEqual(reviewed["unannotated_exceptions"], [])
        self.assertTrue(all(e["status"] == "open" for e in reviewed["exception_register"]["exceptions"]))
        self.assertEqual(verify_pack(pack, digest), report)

    def test_template_is_a_draft_until_reviewer_fills_classification_and_action(self):
        pack, digest = self.unresolved_pack()
        with self.assertRaisesRegex(ValueError, "classification"):
            review_notes(pack["report"], digest, notes_template(pack["report"], digest))

    def test_missing_annotations_are_visible_and_never_resolve_findings(self):
        pack, digest = self.unresolved_pack(); notes = self.filled_notes(pack["report"], digest)
        notes["notes"] = []
        reviewed = review_notes(pack["report"], digest, notes)
        self.assertEqual(len(reviewed["unannotated_exceptions"]), 1)
        self.assertEqual(reviewed["support_status"], "fail")

    def test_stale_unknown_duplicate_accepted_or_blank_notes_are_refused(self):
        pack, digest = self.unresolved_pack(); report = pack["report"]
        mutations = [lambda n: n.update(pack_sha256="0" * 64),
                     lambda n: n["notes"][0].update(exception_id="unknown"),
                     lambda n: n["notes"].append(copy.deepcopy(n["notes"][0])),
                     lambda n: n["notes"][0].update(status="accepted"),
                     lambda n: n["notes"][0].update(rationale=" "),
                     lambda n: n["notes"][0].update(next_action=""),
                     lambda n: n.update(authority="approved"),
                     lambda n: n["notes"][0].update(classification=[])]
        for edit in mutations:
            notes = self.filled_notes(report, digest); edit(notes)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                review_notes(report, digest, notes)

    def test_reviewer_text_is_escaped_in_markdown(self):
        pack, digest = self.unresolved_pack(); report = pack["report"]
        notes = self.filled_notes(report, digest)
        notes["notes"][0]["rationale"] = '<script>x</script> [click](javascript:alert(1))\n|row|'
        md = notes_markdown(report, review_notes(report, digest, notes))
        self.assertNotIn("<script>", md)
        self.assertNotIn("[click](javascript", md)
        self.assertIn("&lt;script&gt;", md)

    def test_real_cli_review_keeps_failure_exit_and_separate_idempotent_artifacts(self):
        pack, digest = self.unresolved_pack(); pack_path = self.root / "pack.json"
        pack_path.write_text(json.dumps(pack)); template = self.root / "notes.template.json"
        self.assertEqual(self.cli("review-template", pack_path, "--expected-sha256", digest,
                                  "--output", template).returncode, 0)
        notes = json.loads(template.read_text())
        for item in notes["notes"]:
            item.update(classification="source_gap", rationale="Missing controlling source",
                        next_action="Obtain and recheck source")
        notes_path = self.root / "notes.v1.json"; notes_path.write_text(json.dumps(notes))
        args = ("review", pack_path, notes_path, "--expected-sha256", digest, "--out", self.root / "reviews")
        first = self.cli(*args); second = self.cli(*args)
        self.assertEqual((first.returncode, second.returncode), (1, 1))
        self.assertEqual(first.stdout, second.stdout)
        receipt = json.loads((Path(json.loads(first.stdout)["output"]) / "review-receipt.json").read_text())
        self.assertEqual(receipt["support_status"], "fail")
        self.assertEqual(json.loads(pack_path.read_text()), pack)
        notes["pack_sha256"] = "0" * 64; notes_path.write_text(json.dumps(notes))
        self.assertEqual(self.cli(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
