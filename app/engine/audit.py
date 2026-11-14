"""Stage 4 — Sign-off and the audit trail.

A sign-off is not a button, it is a record:

  * the exact commentary that was signed, hashed
  * the verification report that cleared it, hashed
  * the source fact store it was checked against, hashed
  * every warning the signer explicitly attested to, with their rationale
  * a hash of the previous sign-off, so the trail cannot be silently rewritten

Re-hashing any of those artefacts after the fact breaks the record.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal


class SignOffRefused(Exception):
    """Raised when the gate, the attestations or the evidence do not permit sign-off."""


def canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(payload) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def signoff_sequence(record: dict) -> int:
    """The chain position of a sign-off record.

    New records persist `sequence` explicitly. Older records (signed before
    the field existed) are still recoverable from the SO-#### identifier, so
    both forms order deterministically without consulting a second-resolution
    timestamp.
    """
    sequence = record.get("sequence")
    if isinstance(sequence, int) and sequence > 0:
        return sequence
    signoff_id = str(record.get("signoff_id", ""))
    prefix, _, suffix = signoff_id.rpartition("-")
    if prefix == "SO" and suffix.isdigit():
        return int(suffix)
    return 0


def _chain_payload(record: dict) -> dict:
    """The exact fields committed to by `chain_hash`.

    `sequence` is part of the payload for records that carry it, so moving a
    record or rewriting its position invalidates the hash. Legacy records keep
    their original payload (no sequence field) and remain verifiable.
    """
    payload = {
        "prev": record["prev_chain_hash"],
        "run_id": record["run_id"],
        "content": record["hashes"]["content_hash"],
        "verification": record["hashes"]["verification_hash"],
        "facts": record["hashes"]["fact_store_hash"],
        "signer": record["signer"],
        "signed_at": record["signed_at"],
    }
    if "sequence" in record:
        payload["sequence"] = record["sequence"]
    return payload


def _variance_core(recon: dict) -> list[dict]:
    fields = (
        "variance_id", "entity", "account", "statement", "period",
        "actual_usd", "budget_usd", "prior_usd", "forecast_usd",
        "variance_usd", "variance_pct", "material", "favorability",
    )
    return [{k: v[k] for k in fields} for v in recon["variances"]]


def hashes(recon: dict, commentary: dict, verification: dict) -> dict:
    content = digest({
        "period": recon["period"],
        "policy": recon["policy"],
        "variances": _variance_core(recon),
        "sections": commentary["sections"],
    })
    verification_hash = digest({
        "status": verification["status"],
        "findings": verification["findings"],
        "stats": verification["stats"],
        "coverage": verification["coverage"],
        "closure": verification["closure"],
        "self_test": verification["self_test"],
    })
    # The fact-store hash commits to each fact's *full* provenance, not just its
    # value: derivation lines (journal ids and amounts), FX rate, opening period,
    # driver legs, aggregate account lists, presentation sign and source ref.
    # Hashing only derivation.type would let a post-signoff edit repoint a fact
    # at different journal lines that happen to total the same value.
    facts_hash = digest(recon["facts"])
    return {
        "content_hash": content,
        "verification_hash": verification_hash,
        "fact_store_hash": facts_hash,
    }


def preflight(recon: dict, verification: dict, attestations: list[dict]) -> dict:
    """Everything that must be true before a signature is accepted."""
    gate = verification["gate"]
    blocks = [f for f in verification["findings"] if f["severity"] == "block"]
    warns = [f for f in verification["findings"] if f["severity"] == "warn"]
    attested = {a.get("finding_id"): (a.get("rationale") or "").strip() for a in attestations}
    unattested = [w["finding_id"] for w in warns if not attested.get(w["finding_id"])]

    checks = [
        {
            "id": "no_blocking",
            "label": "No blocking verification findings",
            "passed": not blocks,
            "detail": f"{len(blocks)} blocking finding(s)" if blocks else "clear",
        },
        {
            "id": "coverage",
            "label": "Every material variance has commentary",
            "passed": verification["stats"]["coverage_covered"] == verification["stats"]["coverage_total"],
            "detail": f"{verification['stats']['coverage_covered']} of {verification['stats']['coverage_total']} covered",
        },
        {
            "id": "figures",
            "label": "Every figure ties to its source figure",
            "passed": verification["stats"]["figures_passed"] == verification["stats"]["figures_checked"],
            "detail": f"{verification['stats']['figures_passed']} of {verification['stats']['figures_checked']} tie out",
        },
        {
            "id": "facts",
            "label": "Every figure re-derives from the ledger, plan and FX tables",
            "passed": verification["stats"]["facts_passed"] == verification["stats"]["facts_checked"],
            "detail": f"{verification['stats']['facts_passed']} of {verification['stats']['facts_checked']} re-derived",
        },
        {
            "id": "closure",
            "label": "Variance attribution reconciles with no unexplained residual",
            "passed": verification["stats"]["closure_passed"] == verification["stats"]["closure_checks"],
            "detail": f"{verification['stats']['closure_passed']} of {verification['stats']['closure_checks']} close",
        },
        {
            "id": "self_test",
            "label": "Verifier control test detected every injected defect",
            "passed": verification["self_test"]["passed"],
            "detail": f"{verification['self_test']['detected']} of {verification['self_test']['total']} mutations caught",
        },
        {
            "id": "attestations",
            "label": "Every warning has been attested by the signer",
            "passed": not unattested,
            "detail": f"{len(warns) - len(unattested)} of {len(warns)} attested" if warns else "no warnings open",
        },
    ]
    permitted = all(c["passed"] for c in checks)
    return {
        "permitted": permitted,
        "checks": checks,
        "unattested": unattested,
        "gate_status": gate["status"],
        "blocking_findings": blocks,
        "warnings": warns,
    }


def sign(
    run: dict,
    recon: dict,
    commentary: dict,
    verification: dict,
    *,
    signer: str,
    role: str,
    note: str,
    attestations: list[dict],
    prev_chain_hash: str,
    sequence: int,
) -> dict:
    if not signer or not signer.strip():
        raise SignOffRefused("A named signer is required.")
    state = preflight(recon, verification, attestations)
    if not state["permitted"]:
        failed = [c["label"] for c in state["checks"] if not c["passed"]]
        raise SignOffRefused("Sign-off is not permitted: " + "; ".join(failed))

    signed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    artefact_hashes = hashes(recon, commentary, verification)
    record = {
        "signoff_id": f"SO-{sequence:04d}",
        "sequence": sequence,
        "run_id": run["id"],
        "period": recon["period"],
        "signer": signer.strip(),
        "role": role.strip() or "Controller",
        "signed_at": signed_at,
        "note": note.strip(),
        "attestations": [
            {
                "finding_id": a["finding_id"],
                "rationale": a["rationale"].strip(),
            }
            for a in attestations
            if (a.get("rationale") or "").strip()
        ],
        "counts": {
            "material_variances": len([v for v in recon["variances"] if v["material"]]),
            "figures_verified": verification["stats"]["figures_checked"],
            "facts_rederived": verification["stats"]["facts_checked"],
            "controls_run": len(recon["integrity"]),
            "warnings_attested": len([f for f in verification["findings"] if f["severity"] == "warn"]),
        },
        "hashes": artefact_hashes,
        "prev_chain_hash": prev_chain_hash,
        "narrative_mode": commentary["mode"],
    }
    record["chain_hash"] = digest(_chain_payload(record))
    return record


def verify_chain(signoffs: list[dict], runs: dict[str, dict] | None = None) -> list[dict]:
    """Recompute every link so the trail can be shown to be intact.

    When the originating runs are supplied, the artefacts as they exist today
    are re-hashed and compared with the hashes captured at signature time, so
    an edit to the commentary, the verification report or the fact store is
    detectable as well as a rewritten chain link.
    """
    runs = runs or {}
    results: list[dict] = []
    previous = "genesis"
    previous_sequence = 0
    # Sequence is the chain key. `signed_at` is wall-clock metadata at second
    # resolution and cannot order two signoffs made in the same second.
    ordered = sorted(signoffs, key=lambda r: (signoff_sequence(r), str(r.get("signoff_id", ""))))
    for record in ordered:
        sequence = signoff_sequence(record)
        expected = digest(_chain_payload(record))
        link_ok = record["prev_chain_hash"] == previous
        hash_ok = expected == record["chain_hash"]
        sequence_ok = (
            sequence == previous_sequence + 1
            and record.get("signoff_id") == f"SO-{sequence:04d}"
        )
        content_ok = True
        source_run = runs.get(record["run_id"])
        if source_run is not None:
            payload = source_run["payload"]
            live = hashes(payload["recon"], payload["commentary"], payload["verification"])
            content_ok = all(
                live[key] == record["hashes"][key]
                for key in ("content_hash", "verification_hash", "fact_store_hash")
            )
        results.append({
            "signoff_id": record["signoff_id"],
            "sequence": sequence,
            "run_id": record["run_id"],
            "signer": record["signer"],
            "signed_at": record["signed_at"],
            "chain_hash": record["chain_hash"],
            "link_ok": link_ok,
            "hash_ok": hash_ok,
            "sequence_ok": sequence_ok,
            "content_ok": content_ok,
            "status": "intact" if (link_ok and hash_ok and sequence_ok and content_ok) else "broken",
        })
        previous = record["chain_hash"]
        previous_sequence = sequence
    return results


def evidence_pack(run: dict, recon: dict, commentary: dict, verification: dict, signoff: dict | None, chain: list[dict]) -> dict:
    return {
        "pack_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engagement": {
            "group": "Acme Global Holdings",
            "period": recon["period"],
            "prior_period": recon["prior_period"],
            "group_currency": recon["group_currency"],
            "run_id": run["id"],
            "narrative_mode": commentary["mode"],
        },
        "materiality_policy": recon["policy"],
        "sources": {
            "ledger": recon["trial_balance"][0] if recon["trial_balance"] else None,
            "fact_count": len(recon["facts"]),
            "control_count": len(recon["integrity"]),
        },
        "variances": recon["variances"],
        "aggregates": recon["aggregates"],
        "facts": recon["facts"],
        "commentary": commentary,
        "verification": {
            "status": verification["status"],
            "gate": verification["gate"],
            "stats": verification["stats"],
            "findings": verification["findings"],
            "fact_checks": verification["fact_checks"],
            "coverage": verification["coverage"],
            "closure": verification["closure"],
            "self_test": verification["self_test"],
        },
        "controls": {
            "integrity": recon["integrity"],
            "intercompany": recon["intercompany"],
            "exceptions": recon["exceptions"],
        },
        "signoff": signoff,
        "chain": chain,
        "manifest": {
            "content_hash": (signoff or {}).get("hashes", {}).get("content_hash"),
            "verification_hash": (signoff or {}).get("hashes", {}).get("verification_hash"),
            "fact_store_hash": (signoff or {}).get("hashes", {}).get("fact_store_hash"),
        },
        "note": "Figure values are stored as decimal strings to preserve exactness. Every fact carries listed derivation information sufficient to recompute it from the journal extract.",
    }


def commentary_csv(recon: dict, commentary: dict) -> str:
    """Review sheet: one row per variance section, figures resolved to source rows."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "section_id", "variance_id", "entity", "entity_name", "account", "account_name",
        "period", "actual_usd", "budget_usd", "variance_usd", "variance_pct",
        "favorability", "material", "figures_cited", "commentary",
    ])
    by_var = {v["variance_id"]: v for v in recon["variances"]}
    for section in commentary["sections"]:
        variance = by_var.get(section.get("variance_id") or "")
        figures = sum(1 for p in section["paragraphs"] for t in p["tokens"] if t["t"] == "fig")
        prose = " ".join(
            "".join(t["v"] if t["t"] == "text" else t["text"] for t in p["tokens"])
            for p in section["paragraphs"]
        )
        writer.writerow([
            section["section_id"],
            section.get("variance_id") or "",
            section["scope"].get("entity") or "",
            (variance or {}).get("entity_name", ""),
            section["scope"].get("account") or "",
            (variance or {}).get("account_name", ""),
            section["scope"].get("period") or "",
            (variance or {}).get("actual_usd", ""),
            (variance or {}).get("budget_usd", ""),
            (variance or {}).get("variance_usd", ""),
            (variance or {}).get("variance_pct", ""),
            (variance or {}).get("favorability", ""),
            "yes" if (variance or {}).get("material") else "no",
            figures,
            prose,
        ])
    return buffer.getvalue()
