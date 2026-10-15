"""Standalone read-only presentation of evaluated accounting facts and retained sources."""
from __future__ import annotations

import base64
import hashlib
import html

STYLE = """:root{color-scheme:light;--ink:#142c37;--muted:#617580;--line:#dce5e8;--paper:#fff;--bg:#f3f6f7;--teal:#11685d;--amber:#8b4b0c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}button{font:inherit;cursor:pointer}button:focus-visible,summary:focus-visible{outline:3px solid #317ea5;outline-offset:3px}[hidden]{display:none!important}
header{background:var(--paper);border-bottom:1px solid var(--line);padding:20px 36px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{font-weight:750;letter-spacing:-.4px}.brand span{background:var(--teal);color:white;border-radius:7px;padding:3px 9px;margin-right:10px}.readonly{font-size:12px;font-weight:650;color:var(--muted)}
.layout{display:grid;grid-template-columns:245px minmax(0,1fr);gap:32px;max-width:1320px;margin:auto;padding:32px 36px}aside{min-width:0}.eyebrow{font-size:11px;font-weight:750;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}.scope{font-weight:650;overflow-wrap:anywhere;margin:8px 0 24px}.claim-nav{display:grid;gap:10px}.claim-button{text-align:left;padding:16px;border:1px solid var(--line);border-radius:10px;background:white;width:100%;color:var(--ink)}.claim-button[aria-pressed=true]{border-color:var(--teal);box-shadow:0 0 0 1px var(--teal)}.claim-button strong{display:block;margin:5px 0;font-size:20px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.small{font-size:12px;color:var(--muted)}.side-note{margin-top:24px;padding-top:20px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
main{min-width:0}h1{font-size:28px;letter-spacing:-.9px;line-height:1.2;margin:0 0 6px}h2{font-size:18px;letter-spacing:-.3px;margin:0}h3{font-size:15px;margin:0 0 12px}.period{color:var(--muted);margin:0 0 24px}.card{background:white;border:1px solid var(--line);border-radius:12px;padding:24px;margin:0 0 20px}.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.badge{display:inline-block;border-radius:5px;font-size:11px;font-weight:750;padding:4px 8px;white-space:nowrap}.supported{background:#e4f3ed;color:var(--teal)}.unresolved{background:#fff0dc;color:var(--amber)}.hero{font-size:42px;line-height:1.15;letter-spacing:-1.5px;font-weight:750;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;margin:12px 0 8px}.support-note{font-size:13px;color:var(--muted);margin:0 0 22px}.bridge{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;align-items:center;gap:12px;border-top:1px solid var(--line);padding-top:20px}.bridge strong{display:block;font-size:20px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.bridge .operator{font-size:24px;color:#8ca0a8}.bridge .end{background:#f0f6f5;border-radius:8px;padding:12px}.metric-grid{display:flex;flex-wrap:wrap;gap:20px;margin:16px 0}.metric-grid strong{display:block;font-size:22px}.table-wrap{overflow-x:auto;margin:0 -2px}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;font-weight:650;color:var(--muted);border-bottom:1px solid var(--line);padding:10px 8px}td{border-bottom:1px solid #edf1f3;padding:12px 8px;vertical-align:top}tr:last-child td{border-bottom:0}.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.source-button{background:none;border:0;color:var(--teal);padding:0;text-decoration:underline;text-underline-offset:3px;text-align:left;overflow-wrap:anywhere}.issue{border-left:3px solid #df9b50;padding:12px 16px;margin:12px 0;background:#fffbf5;border-radius:0 6px 6px 0}.issue p{margin:6px 0;font-size:13px}.issue strong{font-size:13px}.issue-code{font-size:11px;color:var(--amber);overflow-wrap:anywhere}.empty{color:var(--muted);font-size:13px}.filter{width:100%;font:inherit;font-size:13px;border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin:4px 0 10px}.mono{font:11px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.footer{font-size:12px;color:var(--muted);margin:26px 0 12px}.footer p{margin:8px 0}details{margin-top:14px}summary{cursor:pointer;font-size:13px;font-weight:650}dialog{border:1px solid var(--line);border-radius:12px;width:min(800px,calc(100% - 32px));max-height:85vh;padding:24px;color:var(--ink);box-shadow:0 20px 70px #142c3740}dialog::backdrop{background:#142c3760}dialog pre{padding:16px;background:var(--bg);border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}dialog .close{border:1px solid var(--line);border-radius:6px;background:white;padding:5px 12px}.source-path{overflow-wrap:anywhere}
@media(max-width:760px){header{padding:16px 20px;gap:10px}.brand{font-size:14px}.readonly{font-size:10px}.layout{display:block;padding:24px 20px}aside{margin-bottom:24px}.claim-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.scope{margin-bottom:16px}.side-note{display:none}.card{padding:18px}.hero{font-size:36px}.bridge{grid-template-columns:minmax(0,1fr);gap:10px}.bridge>div{display:flex;align-items:center;justify-content:space-between;gap:16px}.bridge .operator{display:none}.bridge strong{font-size:18px}.bridge .end{padding:12px}.period{font-size:13px}.card-head{align-items:flex-start}h1{font-size:26px}.claim-button{padding:12px}.claim-button strong{font-size:18px}}
@media print{header,.claim-nav,.filter,.filter-count,dialog{display:none}body{background:white}.layout{display:block;padding:0}aside .side-note{display:none}.claim-panel[hidden],.issue[hidden]{display:block!important}.card{break-inside:avoid;border:1px solid #ccc}.source-button{color:inherit;text-decoration:none}.footer{font-size:10px}}
"""

SCRIPT = """(() => {
const buttons=[...document.querySelectorAll('[data-claim]')];
const panels=[...document.querySelectorAll('.claim-panel')];
function selectClaim(index){
 buttons.forEach((button,i)=>button.setAttribute('aria-pressed',String(i===index)));
 panels.forEach((panel,i)=>panel.hidden=i!==index);
}
buttons.forEach(button=>button.addEventListener('click',()=>selectClaim(Number(button.dataset.claim))));
selectClaim(0);
document.querySelectorAll('[data-source]').forEach(button=>button.addEventListener('click',()=>document.getElementById('source-'+button.dataset.source).showModal()));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
document.querySelectorAll('.filter').forEach(input=>input.addEventListener('input',()=>{
 const query=input.value.toLowerCase();
 const issues=[...input.closest('.exceptions').querySelectorAll('.issue')];
 issues.forEach(issue=>issue.hidden=!issue.textContent.toLowerCase().includes(query));
 input.closest('.exceptions').querySelector('.filter-count').textContent=issues.filter(issue=>!issue.hidden).length+' of '+issues.length+' open exceptions shown';
}));
})();
"""


def e(value):
    return html.escape(str(value), quote=True)


def usd(cents):
    if cents is None:
        return "Unavailable"
    return f"{'−' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def review_html(pack: dict, digest: str, reviewed: dict | None = None) -> str:
    """Caller supplies a freshly evaluated or verified pack; presentation grants no authority."""
    report = pack["report"]
    files = pack["snapshot"]["files"]
    source_index = {record["id"]: i for i, record in enumerate(files)}
    notes = {note["exception_id"]: note for note in (reviewed or {}).get("notes", [])}
    exceptions = report["exception_register"]["exceptions"]
    nav, panels = [], []
    def source_button(ident):
        if ident not in source_index:
            return e(ident) + " · not retained"
        return f'<button type="button" class="source-button" data-source="{source_index[ident]}">Inspect {e(ident)}</button>'
    def table(headers, rows, label=None):
        head = "".join(f'<th scope="col">{e(label)}</th>' for label in headers)
        attrs = f' tabindex="0" role="region" aria-label="{e(label)}"' if label else ""
        return f'<div class="table-wrap"{attrs}><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    for index, claim in enumerate(report["claims"]):
        status = claim["status"]
        badge = f'<span class="badge {status}">{e(status.capitalize())}</span>'
        nav.append(f'<button type="button" class="claim-button" data-claim="{index}" aria-controls="claim-{index}" aria-pressed="{str(index == 0).lower()}" aria-label="Inspect {e(claim["label"])}"><span>{e(claim["label"])}</span><strong>{usd(claim["ending_cents"])}</strong>{badge}</button>')
        amount_label = "Provisional ending amount" if status == "unresolved" else "Ending amount · retained checks passed"
        parts = [f'<section class="claim-panel" id="claim-{index}" aria-labelledby="claim-heading-{index}">',
                 f'<div class="card"><div class="card-head"><h2 id="claim-heading-{index}">{e(claim["label"])}</h2>{badge}</div>',
                 f'<div class="eyebrow">{amount_label}</div><div class="hero">{usd(claim["ending_cents"])}</div>',
                 '<p class="support-note">Unresolved evidence prevents acceptance of the affected claim. Support describes the retained-input checks.</p>',
                 f'<div class="bridge"><div><span class="small">Opening USD</span><strong>{usd(claim["opening_cents"])}</strong></div><span class="operator">+</span><div><span class="small">Activity USD</span><strong>{usd(claim["activity_cents"])}</strong></div><span class="operator">=</span><div class="end"><span class="small">Ending USD</span><strong>{usd(claim["ending_cents"])}</strong></div></div></div>']
        rows = []
        for leg in claim["trace"]:
            origin = f'record {leg["source_record"]}, lines {leg["source_line_start"]}–{leg["source_line_end"]}' if "source_record" in leg else f'GL record {leg["ledger_row"]}'
            rows.append(f'<tr><td><strong>{e(leg["entry_id"])}</strong><div class="small">Account {e(leg["account"])} · leg {e(leg["leg_id"])}</div></td><td class="num">{usd(leg["signed_cents"])}</td><td>{e(leg["effective_date"])}<div class="small">Recorded {e(leg["recorded_date"])}</div></td><td>{source_button(leg["evidence_id"])}<div class="small">{e(origin)}</div></td></tr>')
        parts += ['<div class="card"><div class="card-head"><h2>Contributing ledger rows</h2></div>', table(["Entry / account", "Amount USD", "Dates", "Evidence / original row"], rows),
                  f'<p class="small">Opening baseline: {source_button(pack["snapshot"]["manifest"]["claims"][index]["opening_evidence_id"])}. Mapped accounts: {e(", ".join(claim["accounts"]))}.</p></div>']
        bank = claim.get("bank_reconciliation")
        if bank:
            settlements = bank.get("settlement_groups", [])
            matched_rows = len(bank["matches"]) + sum(len(group["bank_rows"]) for group in settlements)
            bank_source = pack["snapshot"]["manifest"]["claims"][index]["bank_transactions_id"]
            selected_count = "unavailable" if bank["selected_record_count"] is None else str(bank["selected_record_count"])
            retained_count = "unavailable" if bank["retained_record_count"] is None else str(bank["retained_record_count"])
            parts += [f'<div class="card"><div class="card-head"><h2>Bank transaction coverage</h2><span class="badge {"supported" if bank["status"] == "reconciled" else "unresolved"}">{e(bank["status"].capitalize())}</span></div>',
                      f'<p class="small">Bank opening {usd(bank["bank_opening_cents"])} + rows {usd(bank["bank_activity_cents"])} = {usd(bank["calculated_bank_ending_cents"])}. Reported ending: {usd(bank["reported_bank_ending_cents"])}.</p>',
                      f'<p class="small">Selected {selected_count} of {retained_count} retained bank records. {source_button(bank_source)}. Bank recording history is not supplied.</p>',
                      f'<div class="metric-grid"><div><span class="small">Matched bank rows</span><strong>{matched_rows}</strong></div><div><span class="small">Unmatched bank rows</span><strong>{len(bank["unmatched_bank_rows"])}</strong></div><div><span class="small">Unmatched ledger entries</span><strong>{len(bank["unmatched_ledger_entries"])}</strong></div><div><span class="small">Timing entries</span><strong>{len(bank["timing_ledger_entries"])}</strong></div></div>']
            bank_rows = []
            for match in bank["matches"]:
                row = match["bank_row"]
                bank_rows.append(f'<tr><td>{e(row["transaction_id"])}<div class="small">Posted {e(row["posted_date"])} · original record {row["source_record"]}</div><div class="small">{e(row["reference"])}</div></td><td class="num">{usd(row["amount_cents"])}</td><td>{e(match["entry_id"])}</td><td><span class="badge supported">Bound</span></td></tr>')
            for row in bank["unmatched_bank_rows"]:
                candidate = ", ".join(row["candidate_ledger_entries"]) or "none"
                bank_rows.append(f'<tr><td>{e(row["transaction_id"])}<div class="small">Posted {e(row["posted_date"])} · original record {row["source_record"]}</div><div class="small">{e(row["reference"])}</div></td><td class="num">{usd(row["amount_cents"])}</td><td>{e(candidate)}<div class="small">Amount-only suggestion</div></td><td><span class="badge unresolved">Unmatched</span></td></tr>')
            for group in settlements:
                entries_label = ", ".join(entry["entry_id"] for entry in group["ledger_entries"])
                for row in group["bank_rows"]:
                    bank_rows.append(f'<tr><td>{e(row["transaction_id"])}<div class="small">Posted {e(row["posted_date"])} · original record {row["source_record"]}</div><div class="small">{e(row["reference"])}</div></td><td class="num">{usd(row["amount_cents"])}</td><td>{e(entries_label)}<div class="small">Settlement {e(group["id"])}</div></td><td><span class="badge supported">Group bound</span></td></tr>')
            parts += [table(["Bank row", "Amount USD", "Ledger entry / candidate", "State"], bank_rows),
                      f'<p class="small">Bank roll-forward difference: {usd(bank["bank_rollforward_difference_cents"])}. Adjusted bank minus ledger: {usd(claim["bank_difference_cents"])}. Candidates stay unresolved until explicitly bound with supported evidence.</p>']
            for group in settlements:
                members = "; ".join(f'{entry["entry_id"]}: {usd(entry["amount_cents"])}' for entry in group["ledger_entries"])
                parts.append(f'<details><summary>Settlement {e(group["id"])} · {usd(group["bank_total_cents"])}</summary><p class="small">Bank total {usd(group["bank_total_cents"])} = full ledger entries {usd(group["ledger_total_cents"])}. {e(members)}.</p><p class="small">{e(group["rationale"])}. {source_button(group["evidence_id"])}. Correspondence is a retained preparer assertion.</p></details>')
            if bank["unmatched_ledger_entries"]:
                parts.append('<p class="small">Unmatched ledger: ' + e(", ".join(f'{entry["entry_id"]} ({usd(entry["amount_cents"])})' for entry in bank["unmatched_ledger_entries"])) + '.</p>')
            for row in bank["excluded_bank_rows"]:
                parts.append(f'<p class="small">Outside statement period: {e(row["transaction_id"])} · {usd(row["amount_cents"])} · posted {e(row["posted_date"])} · retained original record {row["source_record"]}.</p>')
            parts.append('</div>')
        elif claim["bank_difference_cents"] is not None:
            parts.append(f'<div class="card"><h2>Bank balance comparison</h2><p>Adjusted bank minus ledger: {usd(claim["bank_difference_cents"])}.</p></div>')
        ar = claim.get("receivables_reconciliation")
        if ar:
            definition = ar["definition"] or {}
            retained_count = "unavailable" if ar["retained_movement_count"] is None else str(ar["retained_movement_count"])
            parts += [f'<div class="card"><div class="card-head"><h2>Invoice receivables support</h2><span class="badge {"supported" if ar["status"] == "reconciled" else "unresolved"}">{e(ar["status"].capitalize())}</span></div>',
                      f'<p class="small">Opening population {usd(ar["opening_population_cents"])} + declared allocations {usd(ar["allocated_activity_cents"])}; reported ending {usd(ar["ending_population_cents"])}.</p>',
                      f'<p class="small">Opening population minus ledger: {usd(ar["opening_minus_ledger_cents"])}. Ending population minus ledger: {usd(ar["ending_minus_ledger_cents"])}.</p>',
                      f'<p class="small">Parsed {ar["parsed_movement_count"]} of {retained_count} retained movement definitions. Population recording history and intraday order are not supplied.</p>',
                      f'<p class="small">{source_button(definition.get("opening_population_id", "Opening population unavailable"))} · {source_button(definition.get("ending_population_id", "Ending population unavailable"))} · {source_button(pack["snapshot"]["manifest"]["claims"][index]["receivables_evidence_id"])}</p>']
            invoice_rows = []
            for invoice in ar["invoices"]:
                presence = "Settled; absent from ending export" if invoice["ending_presence"] == "settled_and_absent" else "Ending record retained" if invoice["ending_presence"] == "retained" else "Ending record missing or unavailable"
                origins = "; ".join(f'{label} record {invoice[label + "_record"]["source_record"]}, lines {invoice[label + "_record"]["source_line_start"]}–{invoice[label + "_record"]["source_line_end"]}' for label in ("opening", "ending") if invoice[label + "_record"])
                invoice_rows.append(f'<tr><td style="min-width:220px"><strong>{e(invoice["invoice_id"])}</strong><div class="small">Customer {e(invoice["customer_id"])} · due {e(invoice["due_date"])}<br>{e(origins)}</div></td><td class="num">{usd(invoice["opening_cents"])}</td><td class="num">{usd(invoice["movement_cents"])}</td><td class="num">{usd(invoice["calculated_ending_cents"])}</td><td class="num">{usd(invoice["reported_ending_cents"])}</td><td><span class="badge {invoice["status"]}">{e(invoice["status"].capitalize())}</span><div class="small">{e(presence)}</div></td></tr>')
            parts += [table(["Invoice / original records", "Opening USD", "Movements USD", "Calculated USD", "Reported USD", "State"], invoice_rows,
                            "Invoice balances; scroll horizontally to inspect all columns")]
            if ar["aging_cents"] is not None:
                labels = {"current": "Current", "1_30": "1–30 days", "31_60": "31–60 days", "61_90": "61–90 days", "91_plus": "91+ days"}
                parts.append('<p class="small">Reported population aging: ' + e("; ".join(f'{labels[key]} {usd(ar["aging_cents"][key])}' for key in labels)) + '. Supplied dates do not establish collectibility or an allowance.</p>')
            movement_rows = [f'<tr><td><strong>{e(row["id"])}</strong><div class="small">{e(row["kind"])} · invoice {e(row["invoice_id"])}</div></td><td class="num">{usd(row["amount_cents"])}</td><td>{e(row["entry_id"])}<div class="small">Effective {e(row["effective_date"])}<br>Recorded {e(row["recorded_date"])}</div></td><td>{source_button(row["evidence_id"])}<div class="small">{e(row["status"])}</div></td></tr>' for row in ar["movements"]]
            parts += ['<h3>Explicit invoice movements</h3>', table(["Movement / invoice", "Amount USD", "Full ledger entry", "Evidence"], movement_rows)]
            for row in ar["invalid_movements"]:
                parts.append(f'<p class="small">Invalid movement record {row["source_record"]}: {e(row["subject"])} · {e(row["detail"])}.</p>')
            for entry in ar["entry_coverage"]:
                parts.append(f'<p class="small">Full entry {e(entry["entry_id"])}: {usd(entry["amount_cents"])}; allocations {usd(entry["allocated_cents"])}; difference {usd(entry["difference_cents"])} · {e(entry["status"])}.</p>')
            parts += ['<p class="small">Bindings and population cutoffs are retained preparer assertions. Positive missing balances, unsupported allocations and identity conflicts prevent acceptance.</p></div>']
        if claim["reconciling_items"]:
            timing_rows = [f'<tr><td>{e(item["id"])}</td><td>{e(item["kind"])}</td><td class="num">{usd(item["bank_adjustment_cents"])}</td><td>{e(item["entry_id"])}<br>{source_button(item["evidence_id"])}</td></tr>' for item in claim["reconciling_items"]]
            parts += ['<div class="card"><h2>Supported timing items</h2>', table(["Item", "Kind", "Bank adjustment USD", "Ledger / evidence"], timing_rows), '</div>']
        claim_exceptions = [item for item in exceptions if item["claim_id"] == claim["id"]]
        parts.append(f'<div class="card exceptions"><div class="card-head"><h2>Open exceptions</h2><span class="badge {"unresolved" if claim_exceptions else "supported"}">{len(claim_exceptions)} open</span></div>')
        if claim_exceptions:
            parts.append(f'<label class="small" for="filter-{index}">Find an exception or next action</label><input id="filter-{index}" class="filter" type="search" placeholder="Search source, issue or owner"><p class="small filter-count" aria-live="polite">{len(claim_exceptions)} of {len(claim_exceptions)} open exceptions shown</p>')
            for item in claim_exceptions:
                note = notes.get(item["id"])
                owner = note["owner"] if note else item["owner"]
                parts.append(f'<article class="issue"><div class="issue-code">{e(item["code"])} · {e(item["subject"])}</div><p><strong>{e(item["rationale"])}</strong></p><p>Owner: {e(owner)} · state: open</p>')
                if note:
                    parts.append(f'<p>Classification: {e(note["classification"])}.</p><p>Reviewer rationale: {e(note["rationale"])}.</p><p><strong>Next action:</strong> {e(note["next_action"])}</p>')
                else:
                    parts.append('<p>No reviewer note yet. Resolve the evidence question and re-run intake.</p>')
                parts.append(f'<div class="mono">{e(item["id"])}</div></article>')
        else:
            parts.append('<p class="empty">No open exceptions in the stated retained-input checks.</p>')
        parts.append('</div></section>')
        panels.append("".join(parts))
    source_rows, dialogs = [], []
    for index, record in enumerate(files):
        state = record["problem"] or "matched"
        source_rows.append(f'<tr><td>{source_button(record["id"])}</td><td class="source-path">{e(record["path"])}</td><td>{e(state)}</td></tr>')
        content = record["content"] if record["content"] is not None else "No readable retained content. This source cannot support a dependent claim."
        dialogs.append(f'<dialog id="source-{index}" aria-labelledby="source-heading-{index}"><div class="card-head"><h2 id="source-heading-{index}">Retained source: {e(record["id"])}</h2><button type="button" class="close" data-close>Close</button></div><p class="small source-path">{e(record["path"])} · {e(state)}</p><p class="mono">Expected SHA256: {e(record["expected_sha256"] or "Not pinned")}<br>Retained SHA256: {e(record["actual_sha256"] or "Unavailable")}</p><pre>{e(content)}</pre></dialog>')
    supported = sum(claim["status"] == "supported" for claim in report["claims"])
    csp_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-{csp_hash}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Accounting evidence · {e(report['scope'])}</title><style>{STYLE}</style></head>
<body><header><div class="brand"><span>=</span>Accounting evidence</div><div class="readonly">READ-ONLY REVIEW · USD</div></header>
<div class="layout"><aside><div class="eyebrow">Retained scope</div><div class="scope">{e(report['scope'])}</div><nav class="claim-nav" aria-label="Statement claims">{''.join(nav)}</nav><p class="side-note">{supported} of {len(report['claims'])} claims supported.<br>{len(exceptions)} open exceptions.<br><br>Reviewer notes assign work. Every evidence failure remains open.</p></aside>
<main><h1>Close evidence review</h1><p class="period">{e(report['period_start'])} through {e(report['period_end'])} · ledger known at {e(report['known_at'])}</p><noscript><p>JavaScript is disabled. All claim sections remain available below; use the retained pack for original source contents.</p></noscript>{''.join(panels)}
<details class="card"><summary>Retained source versions · {len(files)} files</summary>{table(['Source', 'File', 'Revision check'], source_rows)}</details>
<footer class="footer"><p>Pack SHA256 · <span class="mono">{e(digest)}</span></p><p>The opening amount is a retained baseline. This review does not authenticate sources, establish full ledger completeness, provide an accounting opinion or prove client readiness. Bank recording history is not reconstructed.</p><p>Amounts and support states come from the evaluated retained pack. Source corrections require a new reviewed version and fresh intake. This page changes no evidence or acceptance result.</p></footer></main></div>{''.join(dialogs)}<script>{SCRIPT}</script></body></html>'''
