"""Real-pack presentation, exact money, input integrity and escaped-source checks."""
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acfr_factory.accounting_evidence import pack_for
from acfr_factory.accounting_review import notes_template, review_notes
from acfr_factory.accounting_view import SCRIPT, review_html, usd
from acfr_factory.link_contract import fingerprint


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags = []; self.text = []
    def handle_starttag(self, tag, attrs): self.tags.append((tag, dict(attrs)))
    def handle_data(self, data): self.text.append(data)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.example = self.root / "example"
        shutil.copytree(ROOT / "examples/accounting-evidence", self.example)
        self.manifest = self.example / "manifest.bank.json"

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/accounting_evidence.py"), *map(str, args)],
                              text=True, capture_output=True, check=False)

    def retained(self, missing=False):
        if missing: (self.example / "sources/rent.json").unlink()
        pack = pack_for(self.manifest); path = self.root / "pack.json"
        path.write_text(json.dumps(pack)); return pack, fingerprint(pack), path

    def test_exact_money_beyond_browser_number_precision(self):
        self.assertEqual(usd(9_007_199_254_740_993_001), "$90,071,992,547,409,930.01")
        self.assertEqual(usd(-9_007_199_254_740_993_001), "−$90,071,992,547,409,930.01")
        self.assertEqual(usd(None), "Unavailable")
        pack, digest, _ = self.retained()
        pack["report"]["claims"][0]["ending_cents"] = 9_007_199_254_740_993_001
        page = review_html(pack, digest)
        self.assertIn("$90,071,992,547,409,930.01", page)
        self.assertNotIn("toFixed", SCRIPT)

    def test_supported_and_unresolved_claims_stay_distinct_and_all_exceptions_present(self):
        pack, digest, _ = self.retained(missing=True); before = json.dumps(pack, sort_keys=True)
        page = review_html(pack, digest)
        self.assertIn("Provisional ending amount", page)
        self.assertIn("1 of 2 claims supported", page)
        self.assertIn("Retained sales activity", page)
        for item in pack["report"]["exception_register"]["exceptions"]:
            self.assertIn(item["id"], page)
        self.assertIn("No readable retained content", page)
        self.assertEqual(json.dumps(pack, sort_keys=True), before)

    def test_source_and_label_text_cannot_create_active_html(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["claims"][0]["label"] = '& <script>window.__evidence_injected=1</script> "onclick"'
        self.manifest.write_text(json.dumps(manifest))
        source = self.example / "sources/rent.json"
        doc = json.loads(source.read_text()); doc["description"] = '</pre><img src="https://example.invalid/x" onerror="window.__evidence_injected=1">'
        source.write_text(json.dumps(doc))
        pack, digest, _ = self.retained(); page = review_html(pack, digest); parser = Tags(); parser.feed(page)
        self.assertEqual(len([t for t, _ in parser.tags if t == "script"]), 1)
        self.assertFalse(any(t == "img" for t, _ in parser.tags))
        self.assertFalse(any(k.startswith("on") for _, attrs in parser.tags for k in attrs))
        self.assertIn("&lt;script&gt;", page); self.assertIn("&lt;/pre&gt;", page)

    def test_page_has_no_external_assets_or_submission_controls_and_csp_binds_script(self):
        pack, digest, _ = self.retained(); page = review_html(pack, digest); parser = Tags(); parser.feed(page)
        self.assertFalse(any(tag in {"form", "iframe", "link", "img"} for tag, _ in parser.tags))
        self.assertFalse(any("src" in attrs for _, attrs in parser.tags))
        expected = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + expected, page)
        self.assertIn("default-src 'none'", page)

    def test_source_buttons_refer_to_unique_accessible_dialogs_and_claim_regions(self):
        pack, digest, _ = self.retained(); parser = Tags(); parser.feed(review_html(pack, digest))
        ids = [attrs["id"] for _, attrs in parser.tags if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        for tag, attrs in parser.tags:
            if "data-source" in attrs: self.assertIn("source-" + attrs["data-source"], ids)
            if tag == "dialog": self.assertIn(attrs["aria-labelledby"], ids)
            if "aria-controls" in attrs: self.assertIn(attrs["aria-controls"], ids)

    def test_notes_show_next_actions_and_every_exception_remains_open(self):
        pack, digest, _ = self.retained(missing=True); notes = notes_template(pack["report"], digest)
        for item in notes["notes"]:
            item.update(classification="source_gap", owner="Reviewer", rationale="Missing controlling source",
                        next_action="Obtain the source and re-run intake")
        page = review_html(pack, digest, review_notes(pack["report"], digest, notes))
        self.assertIn("Obtain the source and re-run intake", page)
        self.assertIn("Owner: Reviewer · state: open", page)
        self.assertIn("Unresolved", page)
        self.assertTrue(all(item["status"] == "open" for item in pack["report"]["exception_register"]["exceptions"]))

    def test_actual_cli_view_replays_offline_and_retains_failure_exit(self):
        _, digest, path = self.retained(missing=True); shutil.rmtree(self.example)
        out = self.root / "review.html"
        args = ("view", path, "--expected-sha256", digest, "--output", out)
        first = self.cli(*args); second = self.cli(*args)
        self.assertEqual((first.returncode, second.returncode), (1, 1))
        self.assertEqual(first.stdout, second.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result["html_sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertIn("Provisional ending amount", out.read_text())

    def test_cli_refuses_forged_saved_success_and_wrong_pack_digest(self):
        pack, digest, path = self.retained(missing=True)
        pack["report"]["status"] = "pass"; path.write_text(json.dumps(pack))
        for expected in (digest, fingerprint(pack)):
            out = self.root / (expected + ".html")
            result = self.cli("view", path, "--expected-sha256", expected, "--output", out)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())

    def test_cli_rejects_stale_notes_and_refuses_overwriting_changed_artifact(self):
        pack, digest, path = self.retained(missing=True)
        notes = notes_template(pack["report"], "0" * 64); note_path = self.root / "notes.json"
        note_path.write_text(json.dumps(notes)); out = self.root / "review.html"
        result = self.cli("view", path, "--expected-sha256", digest, "--notes", note_path, "--output", out)
        self.assertEqual(result.returncode, 2); self.assertFalse(out.exists())
        out.write_text("Earlier retained view")
        result = self.cli("view", path, "--expected-sha256", digest, "--output", out)
        self.assertEqual(result.returncode, 2); self.assertEqual(out.read_text(), "Earlier retained view")

    def test_explain_keeps_pack_identity_and_returns_content_addressed_html_view(self):
        expected = fingerprint(pack_for(self.manifest))
        result = self.cli("explain", self.manifest, "--out", self.root / "packs")
        self.assertEqual(result.returncode, 0, result.stdout); summary = json.loads(result.stdout)
        self.assertEqual(summary["pack_sha256"], expected)
        page = Path(summary["review_html"])
        self.assertEqual(page.parent.name, summary["html_sha256"])
        self.assertEqual(hashlib.sha256(page.read_bytes()).hexdigest(), summary["html_sha256"])
        self.assertEqual(self.cli("explain", self.manifest, "--out", self.root / "packs").stdout, result.stdout)


if __name__ == "__main__": unittest.main()
