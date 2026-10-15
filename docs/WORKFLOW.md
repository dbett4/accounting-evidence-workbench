# Workflow and data contracts

Run commands from the repository root. Replace paths and digests with the values returned by your own run. Preserve earlier files when preparing a new revision.

## Pin and explain

Copy `examples/accounting-evidence/manifest.template.json` to a new working directory. Supply the declared scope, reporting period, account inventory, opening baseline, source paths, and selected accounts. Source paths resolve relative to the manifest unless absolute paths are explicitly supplied.

After reviewing the controlling sources, write a new pinned manifest:

```bash
python3 scripts/accounting_evidence.py pin path/to/manifest.template.json \
  --output path/to/manifest.v1.json
python3 scripts/accounting_evidence.py explain path/to/manifest.v1.json \
  --out artifacts/reviews
```

Pinning records bytes. It does not approve their accuracy. Outputs are content-addressed, and an existing output must have identical bytes for a repeat run to succeed.

Exit `0` means an evaluated supported result or a successful operation. Exit `1` means unresolved accounting evidence. Exit `2` means invalid inputs or a blocked operation. An evaluable unresolved manifest still produces a pack and review.

## Ledger and source summaries

Normalized ledger CSVs require these unique headers:

```text
entry_id,leg_id,account,debit,credit,unit,effective_date,recorded_date,evidence_id
```

Keep every leg of each included entry. Each leg has one positive debit or credit, and each full entry must balance. Zero debit/credit cells must be explicit. Money is an exact decimal string with no more than two places. Exponents, extra precision, nonfinite values, and silent rounding are refused.

The `accounting-chart/1` source names the scope and accounts. An `accounting-source/1` opening summary binds a claim, its selected accounts, normal side, amount, and period start. Transaction summaries bind an entry ID, USD debit total, and effective date. Bank summaries bind a claim, balance, and period end. The fixture `sources/` directories show complete records.

`effective_date` selects the reporting period. The supplied `recorded_date` and `known_at` cutoff select the known ledger view. Import time never substitutes for missing recording history. An opening balance is an explicit supplied baseline, not a reconstructed historical ledger.

## Map exports explicitly

`manifest.mapped.json` uses a renamed GL export and a separately pinned `accounting-column-map/1` definition. Every required field maps to a source column; every remaining column is explicitly ignored. Ignored values remain retained.

Money supports declared `decimal` or `us_grouped` formats. Ledger-only `blank_as_zero` is an explicit assertion. Dates support `iso`, `mdy_slash`, or `dmy_slash`; the tool does not guess. `recorded_date` needs a source column. A constant USD unit is permitted as an explicit preparer assertion.

Normalization retains the original UTF-8 content, mapping revision, original record numbers, and physical line spans. Unknown conversion rules, truncated rows, duplicate headers, malformed quoting, and unreviewed column additions remain failures.

## Bank and settlement links

`manifest.bank.json` adds a bank CSV, mapping, independent bank opening summary, ending summary, and version-bound links. Each selected bank row needs a supported ledger link. Each selected cash entry needs a supported bank link or separately supported timing evidence. Equal totals do not clear unmatched rows.

Normalized bank headers are:

```text
transaction_id,posted_date,amount,unit,reference
```

Bank amounts are exact signed nonzero decimals. The selected bank period uses posted dates; it does not invent bank recording history. `accounting-bank-links/1` binds explicit one-to-one matches to a basis hash covering source bytes, mappings, scope, accounts, and reporting coordinates.

For a new reviewed basis, draft an empty links file:

```bash
python3 scripts/accounting_evidence.py bank-links-template path/to/manifest.v1.json \
  --claim cash --output path/to/links.template.json
```

Amount-only candidates remain suggestions. Changed sources require new reviewed links. Supported outstanding payments subtract from bank balance, and supported deposits in transit add. Timing items bind a visible supported cash entry and separate evidence; they do not change the books. Prior-period outstanding populations are outside this initial contract.

`manifest.settlement.json` demonstrates one $2,400 bank deposit settling a $2,500 receipt and a separately recorded $100 fee. `accounting-bank-links/2` adds explicit groups and pinned `accounting-bank-settlement/1` support binding each member ID and amount. One bank row may bind several full ledger entries, or several same-direction bank rows may bind one full entry. General many-to-many allocations, zero-net groups, invented fees, duplicate membership, and partial cash-leg allocations are refused.

## Invoice rollforwards

`receivables/manifest.json` checks the opening and ending invoice population against both the ledger and explicit movements. Normalized population CSVs require:

```text
invoice_id,customer_id,issue_date,due_date,amount,unit
```

Amounts are nonnegative exact USD values. IDs are unique within each population, and dates must be valid for the snapshot. An explicit `accounting-receivables-column-map/1` can normalize renamed columns.

The `accounting-receivables/1` schedule binds the two populations, reporting coordinates, current basis hash, and movements. Each issuance or collection allocation has separate `accounting-receivable-movement/1` evidence binding identity, signed amount, dates, and rationale. A full ledger entry's allocations must add up exactly. An invoice may disappear from the ending export only when its calculated balance is zero. Offsetting individual errors remain unresolved even when total receivables ties.

```bash
python3 scripts/accounting_evidence.py receivables-template path/to/manifest.v1.json \
  --claim receivables --output path/to/schedule.template.json
```

Templates start empty. They do not allocate or authenticate transactions. Aging uses supplied due dates and simple date arithmetic; it supplies no collectibility or allowance conclusion.

## Verify, compare, and recheck

Retain the returned `pack_sha256` separately from the pack. It identifies canonical JSON content, not the pretty-printed file bytes. Keeping a digest beside an editable pack without an independent anchor does not establish authenticity.

```bash
python3 scripts/accounting_evidence.py verify path/to/pack.json \
  --expected-sha256 RETAINED_DIGEST
python3 scripts/accounting_evidence.py compare path/to/before.json path/to/after.json \
  --before-sha256 BEFORE_DIGEST --after-sha256 AFTER_DIGEST
python3 scripts/accounting_evidence.py recheck path/to/pack.json \
  --expected-sha256 RETAINED_DIGEST --out artifacts/rechecks
```

Verification recomputes from retained contents after original sources disappear. It checks integrity and equality with current evaluation, including support states. Comparison requires verified packs with the same scope and unit.

Recheck preserves the earlier pack's exact file bytes and derives a distinct current report from the retained inputs. Its receipt binds old and new report fingerprints, the retained snapshot, source-code hashes, and Python version. A code hash identifies local files; it is not an execution attestation. Recheck does not execute code supplied in a pack.

## Record review work

```bash
python3 scripts/accounting_evidence.py review-template path/to/pack.json \
  --expected-sha256 RETAINED_DIGEST --output path/to/notes.template.json
python3 scripts/accounting_evidence.py review path/to/pack.json path/to/notes.v1.json \
  --expected-sha256 RETAINED_DIGEST --out artifacts/reviews
python3 scripts/accounting_evidence.py view path/to/pack.json \
  --expected-sha256 RETAINED_DIGEST --output path/to/review.html \
  --notes path/to/notes.v1.json
```

Each note binds an existing exception and supplies a classification, owner label, rationale, and next action. The template starts incomplete. Supported classifications are `source_gap`, `source_conflict`, `data_error`, `mapping_question`, `timing_question`, and `accounting_judgment`.

Notes do not waive evidence failures or change balances. Missing notes stay counted. Owner labels are not authenticated identities. The HTML page embeds retained sources, so a real-data pack and its page need the same access controls.
