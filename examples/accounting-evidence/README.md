# Authored synthetic cases

All records in this directory were authored as examples. They are not anonymized client exports or independently reviewed accounting conclusions.

From the repository root, substitute a manifest in:

```bash
python3 scripts/accounting_evidence.py explain MANIFEST --out artifacts/reviews
```

| Manifest | Demonstration |
| --- | --- |
| `examples/accounting-evidence/manifest.json` | Normalized GL cash bridge and separate sales claim. |
| `examples/accounting-evidence/manifest.mapped.json` | Explicit column mapping, grouped amounts, slash dates, and multiline source lineage. |
| `examples/accounting-evidence/manifest.bank.json` | Separate bank bridge and explicit source-bound transaction links. |
| `examples/accounting-evidence/manifest.settlement.json` | A net bank deposit linked to a receipt and separately booked fee. |
| `examples/accounting-evidence/receivables/manifest.json` | Invoice issuance, split collection allocations, population rollforward, and aging arithmetic. |

`manifest.template.json` is an unpinned starting definition, not a ready-to-accept example. Copy it and supply reviewed source references before pinning a new version.

Run `python3 scripts/demo.py --out artifacts/demo` for isolated supported and missing-source cases plus offline pack verification. The demo refuses an existing output directory and preserves the fixture files.

The [workflow guide](../../docs/WORKFLOW.md) describes schemas, revision binding, commands, and limits.
