"""Run comparison — what changed between two runs of the same period.

The audit trail tells you a run was signed. This module answers the next
question: *if we ran the close again, what moved?* It walks the two fact
stores, the variance queue, the commentary sections and the verification
findings, and reports:

  * every figure whose value changed, keyed by its semantic identity rather
    than its positional fact id
  * every variance whose actual, budget, variance or materiality changed
  * findings added, cleared or reworded between the two reports
  * commentary sections whose wording or figure citations changed
  * whether an existing signature still covers the comparison run

Nothing here mutates a run or a signature. It is a read model over persisted
payloads, so it can be used on signed and unsigned runs alike.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal

from app.engine.audit import hashes


def _text(value) -> str:
    return "" if value is None else str(value)


def _fact_key(fact: dict) -> tuple:
    derivation = fact.get("derivation") or {}
    return (
        fact.get("kind") or "",
        _text(fact.get("entity")),
        _text(fact.get("account")),
        _text(fact.get("period")),
        _text(fact.get("basis")),
        _text(fact.get("comparator")),
        _text(fact.get("aggregate")),
        _text(derivation.get("driver")),
    )


def _decimal_equal(left, right) -> bool:
    try:
        return Decimal(_text(left)) == Decimal(_text(right))
    except Exception:  # noqa: BLE001 - non-numeric fact values compare as text
        return _text(left) == _text(right)


def _compare_facts(base: dict, compare: dict) -> list[dict]:
    base_index = {_fact_key(f): f for f in base["facts"]}
    compare_index = {_fact_key(f): f for f in compare["facts"]}
    changes: list[dict] = []
    for key in sorted(set(base_index) | set(compare_index)):
        left = base_index.get(key)
        right = compare_index.get(key)
        if left is not None and right is not None:
            if _decimal_equal(left["value"], right["value"]):
                continue
            changes.append({
                "change": "changed",
                "fact_id": right["fact_id"],
                "base_fact_id": left["fact_id"],
                "kind": right["kind"],
                "label": right["label"],
                "entity": right.get("entity"),
                "account": right.get("account"),
                "period": right.get("period"),
                "basis": right.get("basis"),
                "base_value": left["value"],
                "compare_value": right["value"],
                "delta": str(Decimal(right["value"]) - Decimal(left["value"])),
            })
        elif right is not None:
            changes.append({
                "change": "added",
                "fact_id": right["fact_id"],
                "kind": right["kind"],
                "label": right["label"],
                "entity": right.get("entity"),
                "account": right.get("account"),
                "period": right.get("period"),
                "basis": right.get("basis"),
                "base_value": None,
                "compare_value": right["value"],
                "delta": None,
            })
        else:
            changes.append({
                "change": "removed",
                "fact_id": left["fact_id"],
                "base_fact_id": left["fact_id"],
                "kind": left["kind"],
                "label": left["label"],
                "entity": left.get("entity"),
                "account": left.get("account"),
                "period": left.get("period"),
                "basis": left.get("basis"),
                "base_value": left["value"],
                "compare_value": None,
                "delta": None,
            })
    return changes


VARIANCE_FIELDS = (
    "actual_usd", "budget_usd", "prior_usd", "forecast_usd",
    "variance_usd", "variance_pct", "material", "favorability", "direction",
)


def _variance_key(variance: dict) -> tuple:
    return (variance["entity"], variance["account"], variance["period"])


def _compare_variances(base: dict, compare: dict) -> list[dict]:
    base_index = {_variance_key(v): v for v in base["variances"]}
    compare_index = {_variance_key(v): v for v in compare["variances"]}
    rows: list[dict] = []
    for key in sorted(set(base_index) | set(compare_index)):
        left = base_index.get(key)
        right = compare_index.get(key)
        if left is None or right is None:
            source = right or left
            rows.append({
                "change": "added" if left is None else "removed",
                "variance_id": source["variance_id"],
                "base_variance_id": (left or {}).get("variance_id"),
                "entity": source["entity"],
                "account": source["account"],
                "account_short": source["account_short"],
                "fields": [],
            })
            continue
        fields = []
        for field in VARIANCE_FIELDS:
            before, after = left.get(field), right.get(field)
            same = _decimal_equal(before, after) if field not in ("material", "favorability", "direction") else before == after
            if same:
                continue
            entry = {
                "field": field,
                "base": before,
                "compare": after,
            }
            if field.endswith("_usd"):
                try:
                    entry["delta"] = str(Decimal(_text(after)) - Decimal(_text(before)))
                except Exception:  # noqa: BLE001
                    entry["delta"] = None
            fields.append(entry)
        if not fields:
            continue
        rows.append({
            "change": "changed",
            "variance_id": right["variance_id"],
            "base_variance_id": left["variance_id"],
            "entity": right["entity"],
            "account": right["account"],
            "account_short": right["account_short"],
            "fields": fields,
        })
    return rows


def _finding_base_key(finding: dict) -> tuple:
    return (
        finding.get("category") or "",
        finding.get("section_id") or "",
        finding.get("fact_id") or "",
        finding.get("control_id") or "",
    )


def _finding_identity(finding: dict) -> tuple:
    return (
        finding.get("severity") or "",
        finding.get("category") or "",
        finding.get("message") or "",
    )


def _compare_findings(base: dict, compare: dict) -> dict:
    base_groups: dict[tuple, list[dict]] = defaultdict(list)
    compare_groups: dict[tuple, list[dict]] = defaultdict(list)
    for finding in base["findings"]:
        base_groups[_finding_base_key(finding)].append(finding)
    for finding in compare["findings"]:
        compare_groups[_finding_base_key(finding)].append(finding)

    added: list[dict] = []
    cleared: list[dict] = []
    changed: list[dict] = []
    for key in sorted(set(base_groups) | set(compare_groups)):
        left = list(base_groups.get(key, []))
        right = list(compare_groups.get(key, []))
        # Exact matches are unchanged and drop out of the report.
        for finding in list(right):
            match = next((f for f in left if _finding_identity(f) == _finding_identity(finding)), None)
            if match is not None:
                left.remove(match)
                right.remove(finding)
        # Remaining pairs with the same subject were reworded or re-graded.
        while left and right:
            before = left.pop(0)
            after = right.pop(0)
            changed.append({
                "finding_id": after["finding_id"],
                "base_finding_id": before["finding_id"],
                "category": after["category"],
                "section_id": after.get("section_id"),
                "control_id": after.get("control_id"),
                "base_severity": before.get("severity"),
                "compare_severity": after.get("severity"),
                "base_message": before.get("message"),
                "compare_message": after.get("message"),
            })
        for finding in right:
            added.append({
                "finding_id": finding["finding_id"],
                "severity": finding["severity"],
                "category": finding["category"],
                "message": finding["message"],
                "section_id": finding.get("section_id"),
                "fact_id": finding.get("fact_id"),
                "control_id": finding.get("control_id"),
            })
        for finding in left:
            cleared.append({
                "finding_id": finding["finding_id"],
                "severity": finding["severity"],
                "category": finding["category"],
                "message": finding["message"],
                "section_id": finding.get("section_id"),
                "fact_id": finding.get("fact_id"),
                "control_id": finding.get("control_id"),
            })
    return {"added": added, "cleared": cleared, "changed": changed}


def _section_text(section: dict) -> str:
    parts: list[str] = []
    for paragraph in section.get("paragraphs", []):
        for token in paragraph.get("tokens", []):
            parts.append(token.get("v", "") if token.get("t") == "text" else f"[{token.get('text', '')}]")
        parts.append("\n")
    return "".join(parts)


def _section_signature(section: dict) -> str:
    return hashlib.sha256(_section_text(section).encode("utf-8")).hexdigest()


def _section_figures(section: dict) -> list[dict]:
    figures = []
    for paragraph in section.get("paragraphs", []):
        for token in paragraph.get("tokens", []):
            if token.get("t") == "fig":
                figures.append({
                    "fact": token.get("fact"),
                    "text": token.get("text"),
                    "role": paragraph.get("role"),
                })
    return figures


def _compare_sections(base: dict, compare: dict) -> dict:
    base_index = {s["section_id"]: s for s in base["sections"]}
    compare_index = {s["section_id"]: s for s in compare["sections"]}
    changed: list[dict] = []
    added: list[dict] = []
    cleared: list[dict] = []
    for section_id in sorted(set(base_index) | set(compare_index)):
        left = base_index.get(section_id)
        right = compare_index.get(section_id)
        if left is None:
            added.append({
                "section_id": section_id,
                "kind": right["kind"],
                "variance_id": right.get("variance_id"),
                "title": right.get("title"),
            })
            continue
        if right is None:
            cleared.append({
                "section_id": section_id,
                "kind": left["kind"],
                "variance_id": left.get("variance_id"),
                "title": left.get("title"),
            })
            continue
        reasons = []
        if _section_signature(left) != _section_signature(right):
            reasons.append("wording")
        if _section_figures(left) != _section_figures(right):
            reasons.append("figure citations")
        if sorted(left.get("driver_fact_ids") or []) != sorted(right.get("driver_fact_ids") or []):
            reasons.append("driver attribution")
        if not reasons:
            continue
        changed.append({
            "section_id": section_id,
            "kind": right["kind"],
            "variance_id": right.get("variance_id"),
            "title": right.get("title"),
            "reasons": reasons,
        })
    return {"added": added, "cleared": cleared, "changed": changed}


def _run_label(run: dict, recon: dict, verification: dict, signoff: dict | None) -> dict:
    return {
        "run_id": run["id"],
        "period": recon["period"],
        "status": run.get("status"),
        "mode": (run.get("mode") or ""),
        "created_at": run.get("created_at"),
        "label": run.get("label"),
        "engine_version": recon.get("engine_version"),
        "verifier_version": verification.get("verifier_version"),
        "signoff_id": (signoff or {}).get("signoff_id"),
        "content_hash": hashes(recon, run["payload"]["commentary"], verification)["content_hash"],
    }


def diff_runs(
    base_run: dict,
    compare_run: dict,
    *,
    base_signoff: dict | None = None,
    compare_signoff: dict | None = None,
    base_link: dict | None = None,
) -> dict:
    """Compare two persisted runs. `base` is the earlier/left run."""
    base = base_run["payload"]
    compare = compare_run["payload"]
    base_recon, base_commentary, base_verification = (
        base["recon"], base["commentary"], base["verification"],
    )
    compare_recon, compare_commentary, compare_verification = (
        compare["recon"], compare["commentary"], compare["verification"],
    )

    fact_changes = _compare_facts(base_recon, compare_recon)
    variance_changes = _compare_variances(base_recon, compare_recon)
    finding_changes = _compare_findings(base_verification, compare_verification)
    section_changes = _compare_sections(base_commentary, compare_commentary)

    base_hashes = hashes(base_recon, base_commentary, base_verification)
    compare_hashes = hashes(compare_recon, compare_commentary, compare_verification)
    hashes_match = base_hashes == compare_hashes
    base_signed = base_signoff is not None
    base_chain_ok = True if base_link is None else base_link.get("status") == "intact"
    invalidated = base_signed and (not hashes_match or not base_chain_ok)
    if not base_signed:
        reason = "The base run was not signed, so there is no signature to invalidate."
    elif not base_chain_ok:
        reason = "The base run's signature chain link no longer verifies."
    elif not hashes_match:
        reason = (
            "The comparison run has different commentary, verification or fact-store hashes, "
            "so the base signature does not cover it."
        )
    else:
        reason = "The signed artefacts are identical; the base signature still covers this run."

    versions = {
        "base": {
            "engine_version": base_recon.get("engine_version"),
            "verifier_version": base_verification.get("verifier_version"),
        },
        "compare": {
            "engine_version": compare_recon.get("engine_version"),
            "verifier_version": compare_verification.get("verifier_version"),
        },
    }
    versions["engine_changed"] = versions["base"]["engine_version"] != versions["compare"]["engine_version"]
    versions["verifier_changed"] = versions["base"]["verifier_version"] != versions["compare"]["verifier_version"]

    return {
        "base": _run_label(base_run, base_recon, base_verification, base_signoff),
        "compare": _run_label(compare_run, compare_recon, compare_verification, compare_signoff),
        "same_period": base_recon["period"] == compare_recon["period"],
        "period": compare_recon["period"],
        "versions": versions,
        "hashes": {
            "base": base_hashes,
            "compare": compare_hashes,
            "match": hashes_match,
        },
        "signoffs": {
            "base": {
                "present": base_signed,
                "signoff_id": (base_signoff or {}).get("signoff_id"),
                "signer": (base_signoff or {}).get("signer"),
                "signed_at": (base_signoff or {}).get("signed_at"),
                "chain_status": None if base_link is None else base_link.get("status"),
            },
            "compare": {
                "present": compare_signoff is not None,
                "signoff_id": (compare_signoff or {}).get("signoff_id"),
                "signer": (compare_signoff or {}).get("signer"),
                "signed_at": (compare_signoff or {}).get("signed_at"),
            },
            "invalidated": invalidated,
            "reason": reason,
        },
        "summary": {
            "facts_changed": len([f for f in fact_changes if f["change"] == "changed"]),
            "facts_added": len([f for f in fact_changes if f["change"] == "added"]),
            "facts_removed": len([f for f in fact_changes if f["change"] == "removed"]),
            "variances_changed": len(variance_changes),
            "findings_added": len(finding_changes["added"]),
            "findings_cleared": len(finding_changes["cleared"]),
            "findings_changed": len(finding_changes["changed"]),
            "sections_changed": len(section_changes["changed"]),
            "sections_added": len(section_changes["added"]),
            "sections_cleared": len(section_changes["cleared"]),
            "signoff_invalidated": invalidated,
        },
        "facts": fact_changes,
        "variances": variance_changes,
        "findings": finding_changes,
        "sections": section_changes,
    }
