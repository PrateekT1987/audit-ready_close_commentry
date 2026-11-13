"""Stage 3 — Verify.

Nothing the narrator produced is trusted. Three independent layers:

  L1  prose    — every figure on screen must parse back to the number it was
                 rendered from, be cited, and carry a source
  L2  claim    — every citation must belong to the section it appears in and
                 agree with the direction and favour-ability being asserted
  L3  source   — every figure, cited or not, is re-derived from the journal
                 lines, the plan of record and the FX table

A blocking finding stops sign-off. Warnings are disclosed exceptions that the
signer must individually attest to. The module also mutation-tests itself on
every run so the gate can be shown to work rather than merely claimed.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

from app import config
from app.config import ACCOUNTS
from app.engine.reconcile import FactStore, Ledger
from app.money import D, expected_display_number, find_figures_in_text, fmt_money, parse_display, q2

BLOCK = "block"
WARN = "warn"
INFO = "info"

UP_WORDS = ("above", "increase", "increased", "higher", "exceeded", "over plan", "ahead of")
DOWN_WORDS = ("below", "decrease", "decreased", "lower", "short of", "under plan")


class Findings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, severity: str, category: str, message: str, **context) -> dict:
        finding = {
            "finding_id": f"FD-{len(self.items) + 1:03d}",
            "severity": severity,
            "category": category,
            "message": message,
            **{k: v for k, v in context.items() if v is not None},
        }
        self.items.append(finding)
        return finding

    def any_block(self) -> bool:
        return any(f["severity"] == BLOCK for f in self.items)

    def categories(self) -> set[str]:
        return {f["category"] for f in self.items if f["severity"] == BLOCK}


# ---------------------------------------------------------------------------
# L3 — re-derive a fact from the source systems
# ---------------------------------------------------------------------------
def _recompute(fact: dict, ledger: Ledger, dataset: dict, cache: dict[str, Decimal]) -> Decimal:
    derivation = fact["derivation"]
    kind = derivation["type"]

    if kind == "ledger_sum":
        return ledger.presented_usd(derivation["entity"], derivation["account"], derivation["period"])

    if kind == "bs_closing":
        opening = ledger.closing_local(derivation["entity"], derivation["account"], derivation["opening_period"])
        if opening is None:
            raise ValueError(f"no opening balance for {derivation['account']}")
        movement = derivation["presentation_sign"] * ledger.ledger_sum(
            derivation["entity"], derivation["account"], derivation["period"]
        )
        return q2((D(opening) + movement) * D(derivation["rate"]))

    if kind == "plan_lookup":
        row = ledger._plan.get((derivation["entity"], derivation["account"], derivation["period"]))
        if row is None:
            raise ValueError("plan row missing")
        return q2(D(row[f"{derivation['basis']}_local"]) * D(derivation["rate"]))

    if kind in ("difference", "ratio"):
        left_id = derivation["left"] if kind == "difference" else derivation["numerator"]
        right_id = derivation["right"] if kind == "difference" else derivation["denominator"]
        left = cache.get(left_id)
        right = cache.get(right_id)
        if left is None or right is None:
            raise ValueError(f"unresolved dependency {left_id}/{right_id}")
        if kind == "difference":
            return q2(left - right)
        if right == 0:
            return D(0)
        return (left / abs(right)).quantize(Decimal("0.000001"))

    if kind == "driver_sum":
        rows = ledger.drivers(derivation["entity"], derivation["account"], derivation["period"])
        if derivation.get("driver") and derivation["driver"] != "__all__":
            rows = [r for r in rows if r["driver"] == derivation["driver"]]
        amount = sum((D(r["amount_local"]) for r in rows), D(0))
        return q2(amount * D(derivation["rate"]))

    if kind == "driver_category_sum":
        rows = ledger.drivers(derivation["entity"], derivation["account"], derivation["period"])
        controllable = derivation["controllable"]
        rows = [r for r in rows if (r["driver"] not in config.NON_DISCRETIONARY) == controllable]
        amount = sum((D(r["amount_local"]) for r in rows), D(0))
        return q2(amount * D(derivation["rate"]))

    if kind == "aggregate":
        total = D(0)
        for account_code, sign in derivation["accounts"]:
            for entity in dataset["entities"]:
                code = entity["code"]
                basis = derivation["basis"]
                if basis == "actual":
                    value = ledger.presented_actual_usd(code, account_code, derivation["period"])
                elif basis == "prior":
                    value = ledger.presented_actual_usd(code, account_code, ledger.prior)
                else:
                    value = ledger.plan_usd(code, account_code, derivation["period"], basis)
                total += sign * value
        return q2(total)

    raise ValueError(f"unknown derivation {kind}")


def _decimal_places(unit: str) -> str:
    return "0.000001" if unit == "pct" else "0.01"


def _check_facts(recon: dict, ledger: Ledger, dataset: dict, findings: Findings) -> list[dict]:
    cache: dict[str, Decimal] = {}
    results: list[dict] = []
    for fact in recon["facts"]:
        stored = D(fact["value"])
        try:
            recomputed = _recompute(fact, ledger, dataset, cache)
        except Exception as exc:  # noqa: BLE001 - surfaced as an audit finding
            findings.add(
                BLOCK,
                "FACT_UNVERIFIABLE",
                f"{fact['fact_id']} could not be re-derived from source: {exc}",
                fact_id=fact["fact_id"],
                label=fact["label"],
            )
            results.append({"fact_id": fact["fact_id"], "status": "unverifiable", "detail": str(exc)})
            continue
        cache[fact["fact_id"]] = recomputed
        places = _decimal_places(fact["unit"])
        difference = (recomputed - stored).quantize(Decimal(places))
        ok = difference == 0
        if not ok:
            findings.add(
                BLOCK,
                "FACT_UNVERIFIED",
                f"{fact['fact_id']} stores {stored} but source re-derivation returns {recomputed}.",
                fact_id=fact["fact_id"],
                label=fact["label"],
                source=fact["source"],
                detail={"stored": str(stored), "recomputed": str(recomputed), "difference": str(difference)},
            )
        results.append({
            "fact_id": fact["fact_id"],
            "label": fact["label"],
            "kind": fact["kind"],
            "entity": fact["entity"],
            "account": fact["account"],
            "basis": fact["basis"],
            "period": fact["period"],
            "stored": str(stored),
            "recomputed": str(recomputed),
            "status": "pass" if ok else "fail",
            "source": fact["source"],
            "derivation_type": fact["derivation"]["type"],
        })
    return results


# ---------------------------------------------------------------------------
# L1 / L2 — check the commentary
# ---------------------------------------------------------------------------
def _check_sections(
    sections: list[dict],
    facts: dict[str, dict],
    recon: dict,
    findings: Findings,
    *,
    check_coverage: bool = True,
) -> dict:
    variances = {v["variance_id"]: v for v in recon["variances"]}
    stats = {"figures": 0, "figures_passed": 0, "sections": len(sections), "words": 0}
    section_index: dict[str, str] = {}

    for section in sections:
        scope = section["scope"]
        section_index[section["variance_id"] or "summary"] = section["section_id"]
        variance = variances.get(section.get("variance_id"))

        for para_index, paragraph in enumerate(section["paragraphs"]):
            plain_parts: list[str] = []
            cited_drivers: set[str] = set()
            for token in paragraph["tokens"]:
                if token["t"] == "text":
                    plain_parts.append(token["v"])
                    for figure in find_figures_in_text(token["v"]):
                        findings.add(
                            BLOCK,
                            "UNSOURCED_FIGURE",
                            f"Prose in {section['section_id']} states {figure} with no citation to a source figure.",
                            section_id=section["section_id"],
                            paragraph=para_index,
                            claim=figure,
                            snippet=_snippet(token["v"], figure),
                        )
                    continue

                stats["figures"] += 1
                plain_parts.append(token["text"])
                claim = _check_figure(section, para_index, paragraph, token, scope, facts, findings)
                stats.setdefault("claims", []).append(claim)
                if claim["status"] == "pass":
                    stats["figures_passed"] += 1
                fact = facts.get(token["fact"])
                if fact is not None and fact["kind"] == "driver":
                    cited_drivers.add(fact["fact_id"])

            text = "".join(plain_parts)
            stats["words"] += len(text.split())
            _check_word_semantics(section, paragraph, text, variance, findings)

            if paragraph.get("role") == "attribution" and variance is not None:
                driver_facts = {d["fact_id"] for d in variance["drivers"]}
                missing = driver_facts - cited_drivers
                if missing:
                    findings.add(
                        WARN,
                        "DRIVER_UNDISCLOSED",
                        f"{section['section_id']} discloses {len(cited_drivers)} of {len(driver_facts)} attributed "
                        "drivers; the balance of the attribution is not described in the narrative.",
                        section_id=section["section_id"],
                        variance_id=section["variance_id"],
                        detail={"missing_fact_ids": sorted(missing)},
                    )

    # --- coverage: every material variance must have commentary -----------
    coverage_rows: list[dict] = []
    if check_coverage:
        for variance in recon["variances"]:
            if not variance["material"]:
                continue
            section_ids = [s["section_id"] for s in sections if s.get("variance_id") == variance["variance_id"]]
            covered = bool(section_ids)
            coverage_rows.append({
                "variance_id": variance["variance_id"],
                "entity": variance["entity"],
                "entity_name": variance["entity_name"],
                "account": variance["account"],
                "account_short": variance["account_short"],
                "variance_usd": variance["variance_usd"],
                "variance_pct": variance["variance_pct"],
                "favorability": variance["favorability"],
                "section_ids": section_ids,
                "covered": covered,
            })
            if not covered:
                findings.add(
                    BLOCK,
                    "COVERAGE_GAP",
                    f"Material variance {variance['variance_id']} ({variance['entity']} {variance['account_short']}, "
                    f"{variance['variance_usd']} USD) has no commentary.",
                    variance_id=variance["variance_id"],
                    entity=variance["entity"],
                    account=variance["account"],
                )
    else:
        for variance in recon["variances"]:
            if not variance["material"]:
                continue
            coverage_rows.append({
                "variance_id": variance["variance_id"],
                "entity": variance["entity"],
                "account": variance["account"],
                "account_short": variance["account_short"],
                "variance_usd": variance["variance_usd"],
                "variance_pct": variance["variance_pct"],
                "favorability": variance["favorability"],
                "section_ids": [s["section_id"] for s in sections if s.get("variance_id") == variance["variance_id"]],
                "covered": any(s.get("variance_id") == variance["variance_id"] for s in sections),
            })

    stats["coverage"] = coverage_rows
    stats.setdefault("claims", [])
    return stats



def _check_figure(
    section: dict,
    para_index: int,
    paragraph: dict,
    token: dict,
    scope: dict,
    facts: dict[str, dict],
    findings: Findings,
) -> dict:
    """Run every per-figure check and return one claim-ledger row."""
    before = len(findings.items)
    claim_text = token.get("text") or ""
    row = {
        "section_id": section["section_id"],
        "variance_id": section.get("variance_id"),
        "paragraph": para_index,
        "role": paragraph.get("role"),
        "claim": claim_text,
        "fact_id": token.get("fact"),
        "unit": token.get("unit"),
        "sign_mode": token.get("sign_mode", "signed"),
        "fact_label": None,
        "fact_value": None,
        "expected_display": None,
        "status": "pass",
        "categories": [],
    }

    fact = facts.get(token["fact"])
    if fact is None:
        findings.add(
            BLOCK,
            "MISSING_FACT",
            f"{section['section_id']} cites {token['fact']}, which is not in the source fact set.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=token["fact"],
        )
        return _close_claim(row, findings, before)

    row["fact_label"] = fact["label"]
    row["fact_value"] = fact["value"]

    try:
        parsed = parse_display(claim_text)
    except Exception as exc:  # noqa: BLE001
        findings.add(
            BLOCK,
            "UNPARSABLE_FIGURE",
            f"{section['section_id']} renders {claim_text!r}, which is not a readable figure: {exc}.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=fact["fact_id"],
        )
        return _close_claim(row, findings, before)

    effective = abs(D(fact["value"])) if token.get("sign_mode") == "absolute" else D(fact["value"])
    expected = expected_display_number(effective, fact["unit"], token.get("scale", "1"), token.get("decimals", 0))
    row["expected_display"] = str(expected)

    if parsed["unit"] != token.get("unit"):
        findings.add(
            BLOCK,
            "UNIT_MISMATCH",
            f"{section['section_id']} shows {claim_text} as {token.get('unit')} but renders a {parsed['unit']} value.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=fact["fact_id"],
        )

    if D(parsed["number"]) != D(token["number"]):
        findings.add(
            BLOCK,
            "RENDER_INTEGRITY",
            f"{section['section_id']} shows {claim_text} but the rendered payload carries {token['number']} — "
            "the display string was altered after rendering.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=fact["fact_id"],
        )

    if fact["unit"] != token.get("unit"):
        findings.add(
            BLOCK,
            "UNIT_MISMATCH",
            f"{section['section_id']} presents {fact['fact_id']} as a {token.get('unit')} figure; the source "
            f"figure is a {fact['unit']} figure.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=fact["fact_id"],
        )

    if D(parsed["number"]) != expected:
        findings.add(
            BLOCK,
            "VALUE_MISMATCH",
            f"{section['section_id']} states {claim_text} for {fact['fact_id']}; the source figure is "
            f"{_render_like(fact, token)}.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=claim_text,
            fact_id=fact["fact_id"],
            detail={
                "shown": str(parsed["number"]),
                "source_value": fact["value"],
                "expected_display": str(expected),
                "unit": fact["unit"],
                "scale": token.get("scale"),
                "decimals": token.get("decimals"),
                "fact_label": fact["label"],
                "fact_source": fact["source"],
            },
        )

    _check_scope(fact, scope, section, para_index, token, findings)
    return _close_claim(row, findings, before)


def _close_claim(row: dict, findings: Findings, before: int) -> dict:
    raised = findings.items[before:]
    row["categories"] = sorted({f["category"] for f in raised})
    if any(f["severity"] == BLOCK for f in raised):
        row["status"] = "fail"
    elif raised:
        row["status"] = "warn"
    return row


def _render_like(fact: dict, token: dict) -> str:
    """Describe the source figure using the same precision as the claim."""
    from app.money import fmt_money, fmt_pct

    value = D(fact["value"])
    if token.get("sign_mode") == "absolute":
        value = abs(value)
    if fact["unit"] == "pct":
        return fmt_pct(value, token.get("decimals", 1))["text"]
    return fmt_money(value)["text"]


def _check_scope(fact: dict, scope: dict, section: dict, para_index: int, token: dict, findings: Findings) -> None:
    entity = scope.get("entity")
    if entity == "GRP":
        if fact["entity"] != "GRP":
            findings.add(
                BLOCK,
                "SCOPE_MISMATCH",
                f"{section['section_id']} is a group section but cites {fact['fact_id']}, an entity-level figure "
                f"for {fact['entity']}.",
                section_id=section["section_id"],
                paragraph=para_index,
                claim=token["text"],
                fact_id=fact["fact_id"],
            )
    else:
        if fact["entity"] != entity:
            findings.add(
                BLOCK,
                "SCOPE_MISMATCH",
                f"{section['section_id']} is scoped to {entity} but cites {fact['fact_id']}, a figure for "
                f"{fact['entity']}.",
                section_id=section["section_id"],
                paragraph=para_index,
                claim=token["text"],
                fact_id=fact["fact_id"],
            )
        if fact["account"] is not None and scope.get("account") and fact["account"] != scope["account"]:
            findings.add(
                BLOCK,
                "SCOPE_MISMATCH",
                f"{section['section_id']} covers account {scope['account']} but cites {fact['fact_id']}, which "
                f"belongs to account {fact['account']}.",
                section_id=section["section_id"],
                paragraph=para_index,
                claim=token["text"],
                fact_id=fact["fact_id"],
            )

    if fact["comparator"] and fact["comparator"] != scope.get("comparator"):
        findings.add(
            BLOCK,
            "BASIS_MISMATCH",
            f"{section['section_id']} is a {scope.get('comparator')} comparison but cites {fact['fact_id']}, which "
            f"measures the {fact['comparator']} comparator.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=token["text"],
            fact_id=fact["fact_id"],
        )

    if fact["period"] != scope.get("period") and fact["kind"] != "comparator":
        findings.add(
            BLOCK,
            "STALE_PERIOD",
            f"{section['section_id']} covers {scope.get('period')} but cites {fact['fact_id']}, a figure for "
            f"{fact['period']}.",
            section_id=section["section_id"],
            paragraph=para_index,
            claim=token["text"],
            fact_id=fact["fact_id"],
        )


def _check_word_semantics(section: dict, paragraph: dict, text: str, variance: dict | None, findings: Findings) -> None:
    lowered = text.lower()

    if paragraph.get("role") == "movement" and variance is not None:
        value = D(variance["variance_usd"])
        up_hits = [word for word in UP_WORDS if word in lowered]
        down_hits = [word for word in DOWN_WORDS if word in lowered]
        if value > 0 and down_hits and not up_hits:
            findings.add(
                BLOCK,
                "DIRECTION_CONFLICT",
                f"{section['section_id']} describes the movement as '{down_hits[0]}' but the variance is "
                f"{variance['variance_usd']} USD (an increase).",
                section_id=section["section_id"],
                variance_id=section["variance_id"],
                claim=down_hits[0],
            )
        if value < 0 and up_hits and not down_hits:
            findings.add(
                BLOCK,
                "DIRECTION_CONFLICT",
                f"{section['section_id']} describes the movement as '{up_hits[0]}' but the variance is "
                f"{variance['variance_usd']} USD (a decrease).",
                section_id=section["section_id"],
                variance_id=section["variance_id"],
                claim=up_hits[0],
            )

    if variance is not None and paragraph.get("role") == "movement":
        stated_unfav = "unfavorable" in lowered
        stated_fav = "favorable" in lowered and not stated_unfav
        if stated_unfav and variance["favorability"] != "unfavorable":
            findings.add(
                BLOCK,
                "FAVORABILITY_CONFLICT",
                f"{section['section_id']} calls the variance unfavorable but ${variance['variance_usd']} on "
                f"{variance['account_short']} is {variance['favorability']}.",
                section_id=section["section_id"],
                variance_id=section["variance_id"],
                claim="unfavorable",
            )
        if stated_fav and variance["favorability"] != "favorable":
            findings.add(
                BLOCK,
                "FAVORABILITY_CONFLICT",
                f"{section['section_id']} calls the variance favorable but ${variance['variance_usd']} on "
                f"{variance['account_short']} is {variance['favorability']}.",
                section_id=section["section_id"],
                variance_id=section["variance_id"],
                claim="favorable",
            )


def _snippet(text: str, needle: str, width: int = 48) -> str:
    index = text.find(needle)
    if index < 0:
        return text[:width]
    start = max(0, index - width // 2)
    return text[start:start + width].strip()


# ---------------------------------------------------------------------------
# Closure check — attribution must sum to the variance
# ---------------------------------------------------------------------------
def _check_closure(recon: dict, ledger: Ledger, dataset: dict, findings: Findings) -> list[dict]:
    rows: list[dict] = []
    for variance in recon["variances"]:
        if not variance["material"]:
            continue
        period = variance["period"]
        rate = ledger.rate(variance["entity"], period)
        driver_rows = ledger.drivers(variance["entity"], variance["account"], period)
        attributed = q2(sum((D(r["amount_local"]) for r in driver_rows), D(0)) * rate)
        variance_usd = D(variance["variance_usd"])
        difference = q2(attributed - variance_usd)
        ok = difference == 0
        rows.append({
            "variance_id": variance["variance_id"],
            "entity": variance["entity"],
            "account": variance["account"],
            "account_short": variance["account_short"],
            "variance_usd": str(variance_usd),
            "attributed_usd": str(attributed),
            "difference_usd": str(difference),
            "drivers": len(driver_rows),
            "status": "pass" if ok else "fail",
        })
        if not ok:
            findings.add(
                BLOCK,
                "CLOSURE_BREAK",
                f"{variance['variance_id']} attributes {attributed} USD of drivers against a variance of "
                f"{variance_usd} USD; {difference} USD is unexplained.",
                variance_id=variance["variance_id"],
                entity=variance["entity"],
                account=variance["account"],
            )
    return rows


# ---------------------------------------------------------------------------
# Self-test — prove the gate catches the defects it claims to catch
# ---------------------------------------------------------------------------
MUTATIONS = [
    ("scale_error", "A figure restated at the wrong scale (millions rendered as thousands)"),
    ("sign_flip", "A figure restated with the opposite sign"),
    ("rounding_drift", "A figure rounded one step away from the source value"),
    ("unsourced_figure", "A figure introduced into prose with no citation"),
    ("wrong_scope", "A figure borrowed from a different entity"),
    ("basis_swap", "A budget comparison replaced by the prior-period comparison"),
]


def _mutate(sections: list[dict], mutation: str, facts: dict[str, dict], recon: dict) -> dict | None:
    """Return {'sections': ..., 'category_hint': ...} for a mutated copy."""
    mutated = copy.deepcopy(sections)
    target_section = next((s for s in mutated if s["kind"] == "variance"), None)
    if target_section is None:
        return None

    def first_fig(section):
        for paragraph in section["paragraphs"]:
            for token in paragraph["tokens"]:
                if token["t"] == "fig":
                    return paragraph, token
        return None, None

    if mutation == "unsourced_figure":
        target_section["paragraphs"][0]["tokens"].append({
            "t": "text", "v": " A further $40K of accrued spend is expected to unwind.",
        })
        return {"sections": mutated, "hint": "UNSOURCED_FIGURE"}

    if mutation == "wrong_scope":
        variance = next(v for v in recon["variances"] if v["variance_id"] == target_section["variance_id"])
        other = next(
            (f for f in facts.values()
             if f["kind"] == "actual" and f["entity"] != variance["entity"] and f["account"] == variance["account"]
             and f["account"] is not None),
            None,
        )
        if other is None:
            return None
        _, token = first_fig(target_section)
        if token is None:
            return None
        token["fact"] = other["fact_id"]
        return {"sections": mutated, "hint": "SCOPE_MISMATCH"}

    if mutation == "basis_swap":
        variance = next(v for v in recon["variances"] if v["variance_id"] == target_section["variance_id"])
        _, token = first_fig(target_section)
        if token is None:
            return None
        token["fact"] = variance["fact_ids"]["variance_prior"]
        return {"sections": mutated, "hint": "BASIS_MISMATCH"}

    paragraph, token = first_fig(target_section)
    if token is None:
        return None
    fact = facts.get(token["fact"])
    if fact is None:
        return None
    value = D(fact["value"])
    if token.get("sign_mode") == "absolute":
        value = abs(value)
    scale = D(token.get("scale", "1"))
    decimals = token.get("decimals", 0)
    step = Decimal(1).scaleb(-decimals)

    if mutation == "scale_error":
        shown = (value * 1000 / scale).quantize(step)
    elif mutation == "sign_flip":
        shown = -(value / scale).quantize(step)
    elif mutation == "rounding_drift":
        shown = (value / scale).quantize(step) + step
    else:
        return None

    token["number"] = str(shown)
    from app.money import fmt_money, fmt_pct

    token["text"] = fmt_pct(shown / 100, decimals)["text"] if fact["unit"] == "pct" else _money_from_number(shown, token, scale)
    return {"sections": mutated, "hint": "VALUE_MISMATCH"}


def _money_from_number(shown: Decimal, token: dict, scale: Decimal) -> str:
    suffix = ""
    if scale >= 1_000_000_000:
        suffix = "B"
    elif scale >= 1_000_000:
        suffix = "M"
    elif scale >= 1000:
        suffix = "K"
    sign = "-" if shown < 0 else ""
    decimals = token.get("decimals", 2)
    return f"{sign}${abs(shown):,.{decimals}f}{suffix}"


def self_test(sections: list[dict], facts: dict[str, dict], recon: dict) -> dict:
    results: list[dict] = []
    for mutation, description in MUTATIONS:
        mutated = _mutate(sections, mutation, facts, recon)
        if mutated is None:
            results.append({
                "mutation": mutation,
                "description": description,
                "detected": False,
                "categories": [],
                "skipped": True,
            })
            continue
        local = Findings()
        _check_sections(mutated["sections"], facts, recon, local, check_coverage=False)
        categories = sorted(local.categories())
        results.append({
            "mutation": mutation,
            "description": description,
            "detected": bool(categories),
            "categories": categories,
            "expected": mutated["hint"],
            "skipped": False,
        })
    detected = len([r for r in results if r["detected"]])
    return {
        "mutations": results,
        "detected": detected,
        "total": len(results),
        "passed": detected == len(results),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def verify(dataset: dict, recon: dict, commentary: dict) -> dict:
    ledger = Ledger(dataset)
    facts = {f["fact_id"]: f for f in recon["facts"]}
    findings = Findings()

    fact_checks = _check_facts(recon, ledger, dataset, findings)

    # FX rate tamper check: the rate used in a derivation must match treasury.
    for fact in recon["facts"]:
        derivation = fact["derivation"]
        rate = derivation.get("rate")
        if rate is None:
            continue
        currency = derivation.get("currency")
        period = derivation.get("period")
        if not currency or not period:
            continue
        table_rate = ledger.fx.get(period, {}).get(currency)
        if table_rate is None or D(rate) != D(table_rate):
            findings.add(
                BLOCK,
                "FX_RATE_MISMATCH",
                f"{fact['fact_id']} was translated at {rate} but the treasury rate for {currency} in {period} is "
                f"{table_rate}.",
                fact_id=fact["fact_id"],
            )

    stats = _check_sections(commentary["sections"], facts, recon, findings)
    closure = _check_closure(recon, ledger, dataset, findings)
    test = self_test(commentary["sections"], facts, recon)

    if not test["passed"]:
        undetected = [m["mutation"] for m in test["mutations"] if not m["detected"]]
        findings.add(
            BLOCK,
            "SELFTEST_FAILED",
            f"The verifier failed to detect {len(undetected)} injected defect(s): {', '.join(undetected)}. "
            "The verification gate cannot be relied upon.",
            detail={"undetected": undetected},
        )

    for control in recon["integrity"]:
        if control["status"] != "break":
            continue
        severity = control["severity"] if control["severity"] in (BLOCK, WARN) else WARN
        findings.add(
            severity,
            "CONTROL_BREAK",
            f"{control['kind'].replace('_', ' ').capitalize()} break on {control['target']}: "
            f"{fmt_money(control['difference'])['text']} ({control['note']})",
            control_id=control["check_id"],
            detail=control,
        )

    findings.items.sort(key=lambda f: ({BLOCK: 0, WARN: 1, INFO: 2}[f["severity"]], f["category"]))

    blocks = [f for f in findings.items if f["severity"] == BLOCK]
    warns = [f for f in findings.items if f["severity"] == WARN]
    coverage_rows = stats["coverage"]
    covered = len([r for r in coverage_rows if r["covered"]])

    claims = stats.get("claims", [])
    passing = {
        "figures_checked": stats["figures"],
        "figures_passed": stats["figures_passed"],
        "facts_checked": len(fact_checks),
        "facts_passed": len([f for f in fact_checks if f["status"] == "pass"]),
        "closure_checks": len(closure),
        "closure_passed": len([c for c in closure if c["status"] == "pass"]),
        "coverage_total": len(coverage_rows),
        "coverage_covered": covered,
        "sections": stats["sections"],
        "words": stats["words"],
    }

    gate_reasons: list[str] = []
    if blocks:
        gate_reasons.append(f"{len(blocks)} blocking finding(s) must be cleared")
    if covered < len(coverage_rows):
        gate_reasons.append(f"{len(coverage_rows) - covered} material variance(s) lack commentary")
    if warns:
        gate_reasons.append(f"{len(warns)} warning(s) require signer attestation")
    status = "blocked" if blocks or covered < len(coverage_rows) else ("attestation" if warns else "clear")

    return {
        "stage": "verify",
        "status": status,
        "findings": findings.items,
        "claims": claims,
        "fact_checks": fact_checks,
        "closure": closure,
        "coverage": coverage_rows,
        "self_test": test,
        "stats": passing,
        "gate": {
            "status": status,
            "blocking": len(blocks),
            "warnings": len(warns),
            "reasons": gate_reasons,
            "signable": status != "blocked",
        },
    }
