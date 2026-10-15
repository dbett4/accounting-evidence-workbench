#!/usr/bin/env python3
"""Run the real CLI on isolated synthetic records and retain its outputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/accounting_evidence.py"
FIXTURE = ROOT / "examples/accounting-evidence"


def hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(*args, expected: int = 0) -> dict:
    result = subprocess.run([sys.executable, str(CLI), *map(str, args)],
                            capture_output=True, text=True, check=False)
    if result.returncode != expected:
        raise RuntimeError(f"CLI exited {result.returncode}, expected {expected}: {result.stdout}{result.stderr}")
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="A new directory for copied inputs and retained results")
    args = parser.parse_args()
    original = hashes(FIXTURE)
    args.out.mkdir(parents=True, exist_ok=False)
    inputs = args.out / "inputs"
    shutil.copytree(FIXTURE, inputs)
    cases = []
    baseline = None
    for label, manifest, claim, cents in (
        ("cash_bridge", "manifest.json", "cash", 1_090_000),
        ("mapped_gl", "manifest.mapped.json", "cash", 1_090_000),
        ("bank_links", "manifest.bank.json", "cash", 1_090_000),
        ("net_settlement", "manifest.settlement.json", "cash", 1_090_000),
        ("invoice_rollforward", "receivables/manifest.json", "receivables", 160_000),
    ):
        result = run("explain", inputs / manifest, "--out", args.out / "reviews")
        pack = Path(result["output"]) / "pack.json"
        report = json.loads(pack.read_text())["report"]
        target = next(c for c in report["claims"] if c["id"] == claim)
        if result["status"] != "pass" or target["ending_cents"] != cents:
            raise RuntimeError(f"Unexpected authored fixture result for {label}")
        cases.append({"case": label, "status": "pass", "claim": claim,
                      "ending_cents": cents, "pack_sha256": result["pack_sha256"]})
        if label == "cash_bridge":
            baseline = result

    (inputs / "sources/rent.json").unlink()
    unresolved = run("explain", inputs / "manifest.json", "--out", args.out / "reviews", expected=1)
    if unresolved["unresolved_claims"] != ["cash"] or unresolved["supported_claims"] != ["revenue"]:
        raise RuntimeError("Missing source did not preserve the expected claim-level boundary")
    cases.append({"case": "missing_rent", "status": "expected_unresolved", "affected_claim": "cash",
                  "still_supported_claim": "revenue", "pack_sha256": unresolved["pack_sha256"]})

    shutil.rmtree(inputs)
    verified = run("verify", Path(baseline["output"]) / "pack.json",
                   "--expected-sha256", baseline["pack_sha256"])
    if verified["status"] != "verified" or hashes(FIXTURE) != original:
        raise RuntimeError("Retained replay or original fixture preservation failed")
    cases.append({"case": "replay_after_input_removal", "status": "verified"})
    receipt = {"scope": "builder_run_authored_synthetic_cases", "cases": cases,
               "original_fixtures_unchanged": True, "independent_accounting_review": False}
    (args.out / "demo-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
