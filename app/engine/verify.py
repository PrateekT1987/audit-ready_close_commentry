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
import re
from decimal import Decimal

from app import config
from app.config import ACCOUNTS
from app.engine.reconcile import FactStore, Ledger
from app.money import D, expected_display_number, find_figures_in_text, fmt_money, parse_display, q2

BLOCK = "block"
WARN = "warn"
INFO = "info"

# Direction and comparator phrases are parsed as a grammar, not matched as
# loose keywords: `<subject> <verb> <direction phrase> <comparator phrase>`.
# This lets the gate read "in line with plan" and "above the revised plan"
# correctly, including the comparator the prose actually names.
DIRECTION_PHRASES = [
    # the longest phrase at a position wins ("in line with" over "in line")
    ("in line with", "inline"),
    ("on plan", "inline"),
    ("on budget", "inline"),
    ("at plan", "inline"),
    ("flat against", "inline"),
    ("in line", "inline"),
    ("matched plan", "inline"),
    ("ahead of", "up"),
    ("higher than", "up"),
    ("exceeded", "up"),
    ("surpassed", "up"),
    ("above", "up"),
    ("over", "up"),
    ("short of", "down"),
    ("lower than", "down"),
    ("behind", "down"),
    ("missed", "down"),
    ("below", "down"),
    ("under", "down"),
]

COMPARATOR_PHRASES = [
    ("revised forecast", "forecast"),
    ("revised plan", "forecast"),
    ("revised budget", "forecast"),
    ("latest forecast", "forecast"),
    ("plan of record", "budget"),
    ("prior period", "prior"),
    ("prior month", "prior"),
    ("previous period", "prior"),
    ("previous month", "prior"),
    ("prior year", "prior"),
    ("last month", "prior"),
    ("last period", "prior"),
    ("forecast", "forecast"),
    ("planned", "budget"),
    ("budgeted", "budget"),
    ("budget", "budget"),
    ("target", "budget"),
    ("plan", "budget"),
]

# Subjects map to the group aggregate they talk about, so the summary section
# is checked figure by figure rather than as one undifferentiated movement.
SUBJECT_METRICS = [
    ("cost of revenue", "cost_of_revenue"),
    ("net income", "net_income"),
    ("operating income", "operating_income"),
    ("operating expenses", "opex_total"),
    ("operating expense", "opex_total"),
    ("opex", "opex_total"),
    ("gross profit", "gross_profit"),
    ("ebitda", "ebitda"),
    ("revenue", "revenue_total"),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT = re.compile(r"[,;]")
_SUBJECT_SPLIT = re.compile(r"\b(came in at|closed at|came in|closed|was|were|is|are)\b", re.IGNORECASE)


def _direction_matches(sentence: str) -> list[tuple[int, str, str]]:
    """Every direction phrase in the sentence, ordered by position."""
    lowered = sentence.lower()
    matches: dict[int, tuple[str, str]] = {}
    for phrase, direction in DIRECTION_PHRASES:
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
            position = match.start()
            current = matches.get(position)
            if current is None or len(phrase) > len(current[0]):
                matches[position] = (phrase, direction)
    return [(position, *matches[position]) for position in sorted(matches)]


def _comparator_after(sentence: str, end: int) -> tuple[str, str | None]:
    """The comparator phrase immediately following a direction phrase."""
    window = sentence[end:end + 40].lower()
    best: tuple[int, str, str] | None = None
    for phrase, comparator in COMPARATOR_PHRASES:
        match = re.search(rf"\b{re.escape(phrase)}\b", window)
        if match is None:
            continue
        position = match.start()
        if position > 24:
            continue
        if best is None or position < best[0] or (position == best[0] and len(phrase) > len(best[1])):
            best = (position, phrase, comparator)
    if best is None:
        return "", None
    return best[1], best[2]


def _subject_and_verb(segment: str) -> tuple[str, str]:
    clause = _CLAUSE_SPLIT.split(segment.strip(" ,;"))[-1]
    match = _SUBJECT_SPLIT.search(clause)
    if match:
        subject = clause[:match.start()]
        verb = match.group(0)
    else:
        subject = clause
        verb = ""
    subject = re.sub(r"^(?:and|but|so|while|whereas)\s+", "", subject.strip(" ,;:.()"), flags=re.IGNORECASE)
    return subject, verb.lower()


def _metric_for_subject(subject: str) -> str | None:
    lowered = subject.lower()
    for phrase, metric in SUBJECT_METRICS:
        if phrase in lowered:
            return metric
    return None


def extract_direction_claims(text: str) -> list[dict]:
    """Parse direction claims out of prose: subject, direction, comparator.

    Each claim is one `<direction phrase> <comparator phrase>` pair, with the
    subject taken from the clause that introduces it. A phrase pair without a
    comparator is not a claim ("under management's control" says nothing about
    plan). A sentence can carry several claims, which is common in the summary.
    """
    claims: list[dict] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        previous_end = 0
        for start, phrase, direction in _direction_matches(sentence):
            comparator_phrase, comparator = _comparator_after(sentence, start + len(phrase))
            if comparator is not None:
                subject, verb = _subject_and_verb(sentence[previous_end:start])
                claims.append({
                    "sentence": sentence.strip(),
                    "subject": subject,
                    "verb": verb,
                    "metric": _metric_for_subject(subject),
                    "direction": direction,
                    "direction_phrase": phrase,
                    "comparator": comparator,
                    "comparator_phrase": comparator_phrase,
                })
            previous_end = start + len(phrase)
    return claims


def _zero(value: Decimal) -> bool:
    return abs(value) < Decimal("0.005")



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
            _check_semantics(section, paragraph, text, variance, recon, findings)

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


def _expected_claim_variance(claim: dict, variance: dict | None, recon: dict) -> tuple[Decimal | None, str]:
    """The variance the claim is talking about, and a label for the subject."""
    if variance is not None:
        return D(variance["variance_usd"]), variance.get("account_short") or variance["variance_id"]
    metric = claim.get("metric")
    if not metric:
        return None, ""
    aggregate = next((a for a in recon["aggregates"] if a["aggregate_id"] == metric), None)
    if aggregate is None:
        return None, ""
    return D(aggregate["value_usd"]["variance"]), aggregate["label"]


def _check_semantics(
    section: dict,
    paragraph: dict,
    text: str,
    variance: dict | None,
    recon: dict,
    findings: Findings,
) -> None:
    scope = section["scope"]
    for claim in extract_direction_claims(text):
        # The prose names a comparator; it must be the one the section covers.
        if claim["comparator"] and scope.get("comparator") and claim["comparator"] != scope["comparator"]:
            findings.add(
                BLOCK,
                "COMPARATOR_MISMATCH",
                f"{section['section_id']} compares {claim['subject'] or 'the movement'} to the "
                f"{claim['comparator']} comparator ('{claim['comparator_phrase']}'), but the section is "
                f"scoped to the {scope['comparator']} comparator.",
                section_id=section["section_id"],
                variance_id=section.get("variance_id"),
                claim=claim["direction_phrase"],
                detail={
                    "subject": claim["subject"],
                    "verb": claim["verb"],
                    "comparator_phrase": claim["comparator_phrase"],
                    "prose_comparator": claim["comparator"],
                    "scope_comparator": scope.get("comparator"),
                },
            )

        expected, subject_label = _expected_claim_variance(claim, variance, recon)
        if expected is None:
            continue

        direction = claim["direction"]
        if direction == "inline":
            conflict = not _zero(expected)
        elif direction == "up":
            conflict = expected < 0 or _zero(expected)
        else:  # down
            conflict = expected > 0 or _zero(expected)
        if not conflict:
            continue

        findings.add(
            BLOCK,
            "DIRECTION_CONFLICT",
            f"{section['section_id']} describes {subject_label or claim['subject'] or 'the movement'} as "
            f"{claim['direction_phrase']} {claim['comparator_phrase']}, but the variance is "
            f"{expected} USD.",
            section_id=section["section_id"],
            variance_id=section.get("variance_id"),
            claim=claim["sentence"],
            detail={
                "subject": claim["subject"],
                "verb": claim["verb"],
                "direction": direction,
                "direction_phrase": claim["direction_phrase"],
                "comparator_phrase": claim["comparator_phrase"],
                "variance_usd": str(expected),
            },
        )

    if variance is not None and paragraph.get("role") == "movement":
        stated_unfav = "unfavorable" in text.lower()
        stated_fav = "favorable" in text.lower() and not stated_unfav
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
    ("summary_section_scale", "The most-read section restated at the wrong scale"),
    ("two_figure_sign_and_scale", "Two figures in one paragraph changed: one sign-flipped, one rescaled"),
    ("wrong_comparator_and_scope", "One figure on the wrong basis and another from the wrong entity, together"),
    ("prose_comparator_swap", "The prose names a revised plan while the section is scoped to budget"),
]


def _figures(section: dict) -> list[tuple[dict, dict]]:
    return [
        (paragraph, token)
        for paragraph in section["paragraphs"]
        for token in paragraph["tokens"]
        if token["t"] == "fig"
    ]


def _restate(token: dict, fact: dict, mode: str) -> None:
    """Rewrite a figure token's display and payload consistently."""
    value = D(fact["value"])
    if token.get("sign_mode") == "absolute":
        value = abs(value)
    scale = D(token.get("scale", "1"))
    decimals = token.get("decimals", 0)
    step = Decimal(1).scaleb(-decimals)

    if mode == "scale":
        shown = (value * 1000 / scale).quantize(step)
    elif mode == "sign":
        shown = -(value / scale).quantize(step)
    elif mode == "round":
        shown = (value / scale).quantize(step) + step
    else:  # pragma: no cover - internal misuse
        raise ValueError(f"unknown restatement mode {mode!r}")

    token["number"] = str(shown)
    from app.money import fmt_pct

    token["text"] = (
        fmt_pct(shown / 100, decimals)["text"]
        if fact["unit"] == "pct"
        else _money_from_number(shown, token, scale)
    )


def _mutate(sections: list[dict], mutation: str, facts: dict[str, dict], recon: dict) -> dict | None:
    """Return {'sections': ..., 'hint': ...} for a mutated copy."""
    mutated = copy.deepcopy(sections)
    summary = next((s for s in mutated if s["kind"] == "summary"), None)
    target_section = next((s for s in mutated if s["kind"] == "variance"), None)

    if summary is not None and mutation == "summary_section_scale":
        figures = [
            (paragraph, token)
            for paragraph, token in _figures(summary)
            if token["fact"] in facts and D(facts[token["fact"]]["value"]) != 0
        ]
        if not figures:
            return None
        _, token = figures[0]
        fact = facts[token["fact"]]
        _restate(token, fact, "scale")
        return {"sections": mutated, "hint": "VALUE_MISMATCH"}

    if target_section is None:
        return None

    if mutation == "unsourced_figure":
        target_section["paragraphs"][0]["tokens"].append({
            "t": "text", "v": " A further $40K of accrued spend is expected to unwind.",
        })
        return {"sections": mutated, "hint": "UNSOURCED_FIGURE"}

    figures = _figures(target_section)
    if not figures:
        return None

    if mutation == "two_figure_sign_and_scale":
        # Both edits land in the same paragraph, which is where a real model
        # slip would compound: a sign error and a scale error side by side.
        paragraph = next(
            (p for p in target_section["paragraphs"]
             if len([
                 t for t in p["tokens"]
                 if t["t"] == "fig" and t["fact"] in facts and D(facts[t["fact"]]["value"]) != 0
             ]) >= 2),
            None,
        )
        if paragraph is None:
            return None
        tokens = [
            t for t in paragraph["tokens"]
            if t["t"] == "fig" and t["fact"] in facts and D(facts[t["fact"]]["value"]) != 0
        ]
        _restate(tokens[0], facts[tokens[0]["fact"]], "sign")
        _restate(tokens[1], facts[tokens[1]["fact"]], "scale")
        return {"sections": mutated, "hint": "VALUE_MISMATCH"}

    if mutation == "wrong_comparator_and_scope":
        if len(figures) < 2:
            return None
        variance = next(
            (v for v in recon["variances"] if v["variance_id"] == target_section["variance_id"]),
            None,
        )
        if variance is None:
            return None
        other = next(
            (f for f in facts.values()
             if f["kind"] == "actual" and f["entity"] != variance["entity"]
             and f["account"] == variance["account"] and f["account"] is not None),
            None,
        )
        if other is None:
            return None
        # One figure measured against the wrong comparator, one borrowed from
        # another entity. The gate must surface both, not just the first hit.
        figures[0][1]["fact"] = variance["fact_ids"]["variance_prior"]
        figures[1][1]["fact"] = other["fact_id"]
        return {"sections": mutated, "hint": "BASIS_MISMATCH+SCOPE_MISMATCH"}

    if mutation == "prose_comparator_swap":
        for token in target_section["paragraphs"][0]["tokens"]:
            if token["t"] == "text" and " plan" in token["v"]:
                token["v"] = token["v"].replace(" plan", " revised plan", 1)
                return {"sections": mutated, "hint": "COMPARATOR_MISMATCH"}
        return None

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
        figures[0][1]["fact"] = other["fact_id"]
        return {"sections": mutated, "hint": "SCOPE_MISMATCH"}

    if mutation == "basis_swap":
        variance = next(v for v in recon["variances"] if v["variance_id"] == target_section["variance_id"])
        figures[0][1]["fact"] = variance["fact_ids"]["variance_prior"]
        return {"sections": mutated, "hint": "BASIS_MISMATCH"}

    paragraph, token = figures[0]
    fact = facts.get(token["fact"])
    if fact is None:
        return None

    if mutation == "scale_error":
        _restate(token, fact, "scale")
    elif mutation == "sign_flip":
        _restate(token, fact, "sign")
    elif mutation == "rounding_drift":
        _restate(token, fact, "round")
    else:
        return None
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
        local_stats = _check_sections(mutated["sections"], facts, recon, local, check_coverage=False)
        categories = sorted(local.categories())
        failed_figures = len([c for c in local_stats["claims"] if c["status"] == "fail"])
        results.append({
            "mutation": mutation,
            "description": description,
            "detected": bool(categories),
            "categories": categories,
            "failed_figures": failed_figures,
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
