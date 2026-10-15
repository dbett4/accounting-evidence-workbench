"""Pack-bound reviewer annotations; annotations never resolve evidence failures."""
from __future__ import annotations

from .accounting_evidence import markdown_text, review_markdown, text

SCHEMA = "accounting-review-notes/1"
CLASSIFICATIONS = {"source_gap", "source_conflict", "data_error", "mapping_question",
                   "timing_question", "accounting_judgment"}


def notes_template(report: dict, pack_sha256: str) -> dict:
    return {"schema_version": SCHEMA, "pack_sha256": pack_sha256,
            "notes": [{"exception_id": item["id"], "classification": "unclassified",
                       "owner": item["owner"], "rationale": "", "next_action": ""}
                      for item in report["exception_register"]["exceptions"]]}


def review_notes(report: dict, pack_sha256: str, notes: dict) -> dict:
    if (set(notes) != {"schema_version", "pack_sha256", "notes"}
            or notes.get("schema_version") != SCHEMA or notes.get("pack_sha256") != pack_sha256):
        raise ValueError("review notes must bind this exact pack digest and the supported schema")
    if not isinstance(notes["notes"], list):
        raise ValueError("review notes must be a list")
    exceptions = {item["id"]: item for item in report["exception_register"]["exceptions"]}
    seen = set()
    for note in notes["notes"]:
        if (not isinstance(note, dict)
                or set(note) != {"exception_id", "classification", "owner", "rationale", "next_action"}):
            raise ValueError("notes allow only exception_id, classification, owner, rationale and next_action")
        ident = text(note["exception_id"], "exception_id")
        if ident not in exceptions or ident in seen:
            raise ValueError("unknown or duplicate exception in reviewer notes")
        seen.add(ident)
        classification = text(note["classification"], "classification")
        if classification not in CLASSIFICATIONS:
            raise ValueError("choose a supported reviewer classification")
        for field in ("owner", "rationale", "next_action"):
            text(note[field], field)
    return {"schema_version": SCHEMA, "pack_sha256": pack_sha256, "notes": notes["notes"],
            "support_status": report["status"], "annotated_exceptions": sorted(seen),
            "unannotated_exceptions": sorted(exceptions.keys() - seen),
            "exception_register": report["exception_register"],
            "authority": "annotations_only_no_waiver_or_identity_verification"}


def notes_markdown(report: dict, reviewed: dict) -> str:
    lines = [review_markdown(report), "## Reviewer notes", "",
             "These notes classify work and assign next actions. Every evidence exception remains open.",
             "Owner names are supplied labels; this file does not authenticate an approver.", "",
             f"Bound pack SHA256: {reviewed['pack_sha256']}.", ""]
    for note in reviewed["notes"]:
        lines += [f"### {markdown_text(note['exception_id'])}", "",
                  f"Classification: {markdown_text(note['classification'])}; owner: {markdown_text(note['owner'])}.",
                  f"Rationale: {markdown_text(note['rationale'])}",
                  f"Next action: {markdown_text(note['next_action'])}", ""]
    lines += [f"Exceptions without a reviewer note: {len(reviewed['unannotated_exceptions'])}.", ""]
    return "\n".join(lines)
