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
from datetime import date, datetime, timezone
from decimal import Decimal

from app import config


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
    record or rewriting its position invalidates the hash. Identity subjects and
    dual-control approval details are committed only when present, so legacy
    records keep their original payload and remain verifiable.
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
    for key in (
        "sequence",
        "preparer",
        "dual_control",
        "engine_version",
        "verifier_version",
        "signer_subject",
        "signer_issuer",
        "preparer_subject",
        "preparer_issuer",
        "auth_session",
        "dual_control_approval",
    ):
        if key in record:
            payload[key] = record[key]
    return payload


def _variance_core(recon: dict) -> list[dict]:
    fields = (
        "variance_id", "entity", "account", "statement", "period",
        "actual_usd", "budget_usd", "prior_usd", "forecast_usd",
        "variance_usd", "variance_pct", "material", "favorability",
    )
    return [{k: v[k] for k in fields} for v in recon["variances"]]


def hashes(recon: dict, commentary: dict, verification: dict) -> dict:
    content_payload = {
        "period": recon["period"],
        "policy": recon["policy"],
        "variances": _variance_core(recon),
        "sections": commentary["sections"],
    }
    # Version stamps are committed for new runs. Runs signed before they
    # existed carry neither key, and their hashes still recompute unchanged.
    for key in ("engine_version",):
        if recon.get(key):
            content_payload[f"recon_{key}"] = recon[key]
        if commentary.get(key):
            content_payload[f"commentary_{key}"] = commentary[key]
    content = digest(content_payload)
    verification_payload = {
        "status": verification["status"],
        "findings": verification["findings"],
        "stats": verification["stats"],
        "coverage": verification["coverage"],
        "closure": verification["closure"],
        "self_test": verification["self_test"],
    }
    # Cross-section reconciliation is committed when the run has it. Runs signed
    # before this check existed carry no such key, and their hashes still verify.
    if verification.get("cross_section") is not None:
        verification_payload["cross_section"] = verification["cross_section"]
    if verification.get("verifier_version"):
        verification_payload["verifier_version"] = verification["verifier_version"]
    verification_hash = digest(verification_payload)
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


def _attestation_details(attestation: dict) -> dict[str, str]:
    """Structured fields supplied by the signer, from either shape the API accepts."""
    details: dict[str, str] = {}
    for key, value in (attestation.get("details") or {}).items():
        text = str(value).strip() if value is not None else ""
        if text:
            details[str(key)] = text
    # Older clients send the well-known fields at the top level.
    for key in ("control_id", "remediation_owner", "remediation_date", "reviewed_by"):
        value = attestation.get(key)
        text = str(value).strip() if value is not None else ""
        if text:
            details.setdefault(key, text)
    return details


def _validate_attestation(warning: dict, attestation: dict) -> tuple[dict, dict[str, str], list[str]]:
    """Check one warning's attestation against its category template."""
    template = config.attestation_template(warning["category"])
    details = _attestation_details(attestation)
    problems: list[str] = []
    for field in template["fields"]:
        if not field.get("required"):
            continue
        name = field["name"]
        value = details.get(name, "")
        auto = field.get("auto_from_finding")
        if auto:
            expected = str(warning.get(auto) or "").strip()
            if expected and value and value != expected:
                problems.append(f"{field['label']} must be {expected} for this finding, not {value}")
                continue
            if not value and expected:
                value = expected
                details[name] = expected
        if not value:
            problems.append(f"{field['label']} is required")
            continue
        if field.get("type") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                problems.append(f"{field['label']} must be an ISO date (YYYY-MM-DD)")
    return template, details, problems


def preflight(recon: dict, verification: dict, attestations: list[dict]) -> dict:
    """Everything that must be true before a signature is accepted."""
    gate = verification["gate"]
    blocks = [f for f in verification["findings"] if f["severity"] == "block"]
    warns = [f for f in verification["findings"] if f["severity"] == "warn"]
    supplied = {a.get("finding_id"): a for a in attestations}
    attested = {
        finding_id: a for finding_id, a in supplied.items()
        if (a.get("rationale") or "").strip()
    }
    unattested: list[str] = []
    attestation_issues: list[dict] = []
    for warning in warns:
        attestation = attested.get(warning["finding_id"])
        if attestation is None:
            unattested.append(warning["finding_id"])
            continue
        template, _, problems = _validate_attestation(warning, attestation)
        if problems:
            unattested.append(warning["finding_id"])
            attestation_issues.append({
                "finding_id": warning["finding_id"],
                "category": warning["category"],
                "template": template["label"],
                "problems": problems,
            })

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
            "id": "cross_section",
            "label": "Cross-section driver arithmetic reconciles",
            "passed": (
                verification.get("cross_section") is None
                or verification["cross_section"]["passed"] == verification["cross_section"]["total"]
            ),
            "detail": (
                "not recorded for this run"
                if verification.get("cross_section") is None
                else f"{verification['cross_section']['passed']} of "
                     f"{verification['cross_section']['total']} driver and claim checks tie"
            ),
        },
        {
            "id": "self_test",
            "label": "Verifier control test detected every injected defect",
            "passed": verification["self_test"]["passed"],
            "detail": f"{verification['self_test']['detected']} of {verification['self_test']['total']} mutations caught",
        },
        {
            "id": "attestations",
            "label": "Every warning is attested with its category's required detail",
            "passed": not unattested,
            "detail": f"{len(warns) - len(unattested)} of {len(warns)} attested" if warns else "no warnings open",
        },
    ]
    permitted = all(c["passed"] for c in checks)
    return {
        "permitted": permitted,
        "checks": checks,
        "unattested": unattested,
        "attestation_issues": attestation_issues,
        "gate_status": gate["status"],
        "blocking_findings": blocks,
        "warnings": warns,
    }


def _validate_dual_control(
    approval: dict | None,
    *,
    run_id: str,
    preparer_subject: str,
    signed_at: str,
) -> dict:
    """Validate the admin-approved exception bound into a dual-control sign-off."""
    if not isinstance(approval, dict):
        raise SignOffRefused(
            "Dual control requires an admin-approved exception for this run; "
            "a caller-supplied flag is not accepted."
        )
    required = ("approval_id", "approved_subject", "approved_by", "expires_at", "reason")
    missing = [key for key in required if not str(approval.get(key, "")).strip()]
    if missing:
        raise SignOffRefused(
            "The dual-control approval is incomplete: " + ", ".join(missing) + "."
        )
    if approval.get("run_id") and approval["run_id"] != run_id:
        raise SignOffRefused("The dual-control approval belongs to a different run.")
    if approval["approved_subject"] == preparer_subject:
        raise SignOffRefused("The dual-control approver must be different from the preparer.")
    expires = approval.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires))
    except ValueError as exc:
        raise SignOffRefused("The dual-control approval expiry is not a valid timestamp.") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    signed = datetime.fromisoformat(signed_at)
    if signed.tzinfo is None:
        signed = signed.replace(tzinfo=timezone.utc)
    if expires_at <= signed:
        raise SignOffRefused("The dual-control approval has expired.")
    return approval


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
    preparer: str = "",
    dual_control: bool = False,
    signer_subject: str = "",
    signer_issuer: str = "",
    preparer_subject: str = "",
    preparer_issuer: str = "",
    dual_control_approval: dict | None = None,
    session_id: str = "",
) -> dict:
    signer_name = signer.strip() if signer else ""
    preparer_name = preparer.strip() if preparer else ""
    signer_subject = (signer_subject or "").strip()
    preparer_subject = (preparer_subject or "").strip()

    if not signer_name:
        raise SignOffRefused("A named signer is required.")
    if not preparer_name:
        raise SignOffRefused("A preparer of record is required for separation of duties.")
    if signer_subject and not preparer_subject:
        raise SignOffRefused(
            "This run has no attributed preparer subject; re-open the run before signing."
        )

    if signer_subject and preparer_subject:
        same_person = signer_subject == preparer_subject
    else:
        # Legacy engine callers predate subjects. Name comparison remains the
        # fallback so historical records and unit tests keep their behaviour.
        same_person = signer_name.casefold() == preparer_name.casefold()

    if same_person and not dual_control:
        raise SignOffRefused(
            "Separation of duties: the signer must be different from the preparer "
            f"({preparer_name}). Dual control needs an admin-approved exception."
        )

    signed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    used_dual_control = bool(same_person and dual_control)
    if used_dual_control and signer_subject and preparer_subject:
        dual_control_approval = _validate_dual_control(
            dual_control_approval,
            run_id=run["id"],
            preparer_subject=preparer_subject,
            signed_at=signed_at,
        )

    state = preflight(recon, verification, attestations)
    if not state["permitted"]:
        failed = [c["label"] for c in state["checks"] if not c["passed"]]
        message = "Sign-off is not permitted: " + "; ".join(failed)
        if state.get("attestation_issues"):
            detail = "; ".join(
                f"{issue['finding_id']} ({issue['category']}): {', '.join(issue['problems'])}"
                for issue in state["attestation_issues"]
            )
            message += f". Attestation detail required: {detail}"
        raise SignOffRefused(message)

    artefact_hashes = hashes(recon, commentary, verification)
    supplied = {a.get("finding_id"): a for a in attestations}
    witnessed: list[dict] = []
    for warning in state["warnings"]:
        attestation = supplied[warning["finding_id"]]
        template, details, _ = _validate_attestation(warning, attestation)
        entry = {
            "finding_id": warning["finding_id"],
            "category": warning["category"],
            "template": template["label"],
            "rationale": attestation["rationale"].strip(),
            "details": details,
        }
        for key in ("control_id", "remediation_owner", "remediation_date", "reviewed_by"):
            if details.get(key):
                entry[key] = details[key]
        witnessed.append(entry)

    record = {
        "signoff_id": f"SO-{sequence:04d}",
        "sequence": sequence,
        "run_id": run["id"],
        "period": recon["period"],
        "signer": signer_name,
        "preparer": preparer_name,
        "dual_control": used_dual_control,
        "role": role.strip() or "Controller",
        "signed_at": signed_at,
        "note": note.strip(),
        "engine_version": recon.get("engine_version") or config.ENGINE_VERSION,
        "verifier_version": verification.get("verifier_version") or config.VERIFIER_VERSION,
        "attestations": witnessed,
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
    # Identity evidence is committed only when present, preserving the exact
    # hash payload of records signed before attributed identity existed.
    for key, value in (
        ("signer_subject", signer_subject),
        ("signer_issuer", (signer_issuer or "").strip()),
        ("preparer_subject", preparer_subject),
        ("preparer_issuer", (preparer_issuer or "").strip()),
        ("auth_session", (session_id or "").strip()),
    ):
        if value:
            record[key] = value
    if used_dual_control and dual_control_approval:
        record["dual_control_approval"] = dual_control_approval
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


def pack_id_for(run: dict, recon: dict, commentary: dict, verification: dict, signoff: dict | None) -> str:
    """A stable identifier for the evidence in a run.

    Unlike `generated_at` and the current chain snapshot, this value does not
    move between downloads, so two packs of the same run can be shown to
    describe the same signed artefacts.
    """
    if signoff is not None:
        material = {
            "run_id": run["id"],
            "signoff_id": signoff.get("signoff_id"),
            "sequence": signoff.get("sequence"),
            "hashes": signoff.get("hashes"),
            "chain_hash": signoff.get("chain_hash"),
        }
    else:
        material = {
            "run_id": run["id"],
            "period": recon["period"],
            "status": verification["status"],
            "hashes": hashes(recon, commentary, verification),
        }
    return digest({
        "kind": "ledgerline.evidence_pack",
        "pack_version": config.EVIDENCE_PACK_VERSION,
        **material,
    })


def _pack_hash(pack: dict) -> str:
    """Hash of the pack as downloaded, excluding the hash itself."""
    return digest({key: value for key, value in pack.items() if key != "pack_hash"})


def verify_pack_hash(pack: dict) -> bool:
    """True when the downloaded bytes still match the pack's own digest."""
    return bool(pack.get("pack_hash")) and _pack_hash(pack) == pack["pack_hash"]


def evidence_pack(run: dict, recon: dict, commentary: dict, verification: dict, signoff: dict | None, chain: list[dict]) -> dict:
    pack_id = pack_id_for(run, recon, commentary, verification, signoff)
    pack = {
        "pack_version": config.EVIDENCE_PACK_VERSION,
        "pack_id": pack_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engagement": {
            "group": "Acme Global Holdings",
            "period": recon["period"],
            "prior_period": recon["prior_period"],
            "group_currency": recon["group_currency"],
            "run_id": run["id"],
            "narrative_mode": commentary["mode"],
            "engine_version": recon.get("engine_version") or config.ENGINE_VERSION,
            "verifier_version": verification.get("verifier_version") or config.VERIFIER_VERSION,
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
            "cross_section": verification.get("cross_section"),
            "self_test": verification["self_test"],
            "engine_version": verification.get("engine_version") or config.ENGINE_VERSION,
            "verifier_version": verification.get("verifier_version") or config.VERIFIER_VERSION,
        },
        "controls": {
            "integrity": recon["integrity"],
            "intercompany": recon["intercompany"],
            "exceptions": recon["exceptions"],
        },
        "signoff": signoff,
        "chain": chain,
        "manifest": {
            "pack_id": pack_id,
            "pack_version": config.EVIDENCE_PACK_VERSION,
            "signed": signoff is not None,
            "hash_rule": "sha256 of this pack as downloaded, with pack_hash removed",
            "engine_version": recon.get("engine_version") or config.ENGINE_VERSION,
            "verifier_version": verification.get("verifier_version") or config.VERIFIER_VERSION,
            "content_hash": (signoff or {}).get("hashes", {}).get("content_hash"),
            "verification_hash": (signoff or {}).get("hashes", {}).get("verification_hash"),
            "fact_store_hash": (signoff or {}).get("hashes", {}).get("fact_store_hash"),
            "signoff_id": (signoff or {}).get("signoff_id"),
            "chain_hash": (signoff or {}).get("chain_hash"),
        },
        "note": "Figure values are stored as decimal strings to preserve exactness. Every fact carries listed derivation information sufficient to recompute it from the journal extract.",
    }
    pack["pack_hash"] = _pack_hash(pack)
    return pack


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
