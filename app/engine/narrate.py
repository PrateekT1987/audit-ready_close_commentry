"""Stage 2 — Narrate.

Produces the close commentary as a *claim plan*: the prose is composed of text
runs and figure spans, and every figure span cites a fact id from stage 1. The
narrator never writes a number itself — it selects a fact and a precision, and
the renderer turns the fact into text. That is what makes stage 3 possible.

Two modes:

  strict  — the deterministic template engine (default)
  drift   — the same plan with two realistic model-drift defects injected:
            a figure that appears in prose without provenance, and a figure
            citing the wrong period/basis. Used in the product as an
            adversarial test of the verification gate.
"""
from __future__ import annotations

from decimal import Decimal

from app import config
from app.config import ACCOUNTS
from app.money import D, fmt_money, fmt_pct

MODE_LABELS = {
    "strict": "Deterministic template (fact-constrained)",
    "drift": "Simulated model drift (adversarial test)",
}


# ---------------------------------------------------------------------------
# Paragraph builder
# ---------------------------------------------------------------------------
class Paragraph:
    def __init__(self, role: str) -> None:
        self.role = role
        self.tokens: list[dict] = []

    def t(self, text: str) -> "Paragraph":
        if text:
            self.tokens.append({"t": "text", "v": text})
        return self

    def money(self, fact: dict, absolute: bool = False) -> "Paragraph":
        value = abs(D(fact["value"])) if absolute else D(fact["value"])
        rendered = fmt_money(value)
        self.tokens.append({
            "t": "fig", "fact": fact["fact_id"],
            "sign_mode": "absolute" if absolute else "signed",
            **rendered,
        })
        return self

    def pct(self, fact: dict, decimals: int = 1, absolute: bool = False) -> "Paragraph":
        value = abs(D(fact["value"])) if absolute else D(fact["value"])
        rendered = fmt_pct(value, decimals)
        self.tokens.append({
            "t": "fig", "fact": fact["fact_id"],
            "sign_mode": "absolute" if absolute else "signed",
            **rendered,
        })
        return self

    def to_dict(self) -> dict:
        return {"role": self.role, "tokens": self.tokens}

    def plain_text(self) -> str:
        return "".join(tok["v"] if tok["t"] == "text" else tok["text"] for tok in self.tokens)


def _account_noun(account: str) -> str:
    meta = ACCOUNTS[account]
    return meta.phrase or meta.name


def _direction(value) -> str:
    return "above" if D(value) > 0 else "below"


def _join_clauses(clauses: list[str]) -> str:
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    if len(clauses) == 2:
        return f"{clauses[0]} and {clauses[1]}"
    return ", ".join(clauses[:-1]) + f" and {clauses[-1]}"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
def _summary_section(agg: dict[str, dict], facts: dict[str, dict], period: str) -> dict:
    revenue = agg["revenue_total"]
    gross = agg["gross_profit"]
    opex = agg["opex_total"]
    oi = agg["operating_income"]
    ebitda = agg["ebitda"]
    ni = agg["net_income"]
    period_label = config.PERIOD_LABELS[period]

    def var_line(paragraph: Paragraph, block: dict) -> Paragraph:
        paragraph.t(" by ").money(facts[block["fact_ids"]["variance"]], absolute=True)
        paragraph.t(" (").pct(facts[block["fact_ids"]["variance_pct"]], absolute=True).t(")")
        return paragraph

    p1 = Paragraph("movement")
    p1.t("Group revenue for " + period_label + " was ").money(facts[revenue["fact_ids"]["actual"]])
    p1.t(f", {_direction(revenue['value_usd']['variance'])} plan of ").money(facts[revenue["fact_ids"]["budget"]])
    var_line(p1, revenue)
    p1.t(". Gross profit of ").money(facts[gross["fact_ids"]["actual"]])
    p1.t(f" was {_direction(gross['value_usd']['variance'])} plan by ").money(facts[gross["fact_ids"]["variance"]], absolute=True)
    p1.t(" as the revenue effect was partly absorbed by cost of revenue.")

    p2 = Paragraph("movement")
    p2.t("Operating expenses of ").money(facts[opex["fact_ids"]["actual"]])
    p2.t(f" were {_direction(opex['value_usd']['variance'])} plan by ").money(facts[opex["fact_ids"]["variance"]], absolute=True)
    p2.t(" (").pct(facts[opex["fact_ids"]["variance_pct"]], absolute=True)
    p2.t("), so operating income closed at ").money(facts[oi["fact_ids"]["actual"]])
    p2.t(f", {_direction(oi['value_usd']['variance'])} the planned ").money(facts[oi["fact_ids"]["budget"]])
    var_line(p2, oi)
    p2.t(".")

    p3 = Paragraph("quality")
    p3.t("EBITDA of ").money(facts[ebitda["fact_ids"]["actual"]])
    p3.t(" carries the same movement, and net income closed at ").money(facts[ni["fact_ids"]["actual"]])
    p3.t(f", {_direction(ni['value_usd']['variance'])} plan by ").money(facts[ni["fact_ids"]["variance"]], absolute=True)
    p3.t(". The entity variances set out below are the material movements behind the group gap to plan; items below the review threshold are not individually narrated.")

    return {
        "section_id": "S-01",
        "kind": "summary",
        "variance_id": None,
        "scope": {"entity": "GRP", "account": None, "period": period, "comparator": "budget"},
        "heading": "Group performance",
        "eyebrow": f"{config.GROUP_NAME} · consolidated · {period_label} · USD",
        "paragraphs": [p1.to_dict(), p2.to_dict(), p3.to_dict()],
        "favorability": "unfavorable" if D(oi["value_usd"]["variance"]) < 0 else "favorable",
        "driver_fact_ids": [],
    }


def _variance_section(variance: dict, facts: dict[str, dict], index: int) -> dict:
    account = variance["account"]
    entity_name = variance["entity_name"]
    period_label = config.PERIOD_LABELS[variance["period"]]
    ids = variance["fact_ids"]
    actual = facts[ids["actual"]]
    budget = facts[ids["budget"]]
    var_fact = facts[ids["variance"]]
    pct_fact = facts[ids["variance_pct"]]
    variance_usd = D(variance["variance_usd"])
    above = variance_usd > 0
    direction_word = "above" if above else "below"
    noun = _account_noun(account)
    is_bs = variance["statement"] == "BS"

    # -- paragraph 1: the position ----------------------------------------
    p1 = Paragraph("movement")
    if is_bs:
        p1.t(f"{noun} closed at ").money(actual)
        p1.t(f", {direction_word} the planned ").money(budget)
        p1.t(" by ").money(var_fact, absolute=True)
        p1.t(" (").pct(pct_fact, absolute=True)
        p1.t("). The movement is a working-capital variance and carries no profit and loss effect.")
    else:
        p1.t(f"{noun} came in at ").money(actual)
        p1.t(f", {direction_word} the plan of ").money(budget)
        p1.t(" by ").money(var_fact, absolute=True)
        p1.t(" (").pct(pct_fact, absolute=True)
        p1.t(").")
        if variance["favorability"] == "unfavorable":
            p1.t(" The variance is unfavorable and reduces operating income for the period.")
        elif variance["favorability"] == "favorable":
            p1.t(" The variance is favorable and supports operating income for the period.")

    # -- paragraph 2: attribution -----------------------------------------
    p2 = Paragraph("attribution")
    drivers = [d for d in variance["drivers"] if D(d["amount"]) != 0]
    if drivers:
        # The largest attributed driver is named explicitly, so the prose makes
        # a claim the verifier can check against the arithmetic.
        primary = drivers[0]
        p2.t("Attribution: ")
        p2.t(f"{primary['cause']} is the primary driver, contributing ")
        if D(primary["amount"]) >= 0:
            p2.money(facts[primary["fact_id"]])
        else:
            p2.t("an offset of ").money(facts[primary["fact_id"]], absolute=True)
        rest = drivers[1:]
        if rest:
            p2.t("; ")
            for position, driver in enumerate(rest):
                if position:
                    p2.t(", " if position < len(rest) - 1 else " and ")
                p2.t(f"{driver['cause']} contributes ")
                if D(driver["amount"]) >= 0:
                    p2.money(facts[driver["fact_id"]])
                else:
                    p2.t("an offset of ").money(facts[driver["fact_id"]], absolute=True)
        p2.t(". The attribution sums exactly to the variance, leaving no unexplained residual.")
    else:
        p2.t("No driver attribution has been recorded against this variance; the movement is treated as unexplained and requires management input.")

    # -- paragraph 3: quality of the movement ------------------------------
    p3 = Paragraph("quality")
    discret_id = variance.get("category_fact_ids", {}).get("discretionary")
    nondiscret_id = variance.get("category_fact_ids", {}).get("nondiscretionary")
    if discret_id and nondiscret_id:
        discret = D(facts[discret_id]["value"])
        nondiscret = D(facts[nondiscret_id]["value"])

        def split_clause(fact_id: str, value: Decimal, label: str) -> Paragraph:
            if value >= 0:
                p3.money(facts[fact_id]).t(f" sits in {label}")
            else:
                p3.t("an offset of ").money(facts[fact_id], absolute=True).t(f" sits in {label}")
            return p3

        if discret != 0 and nondiscret != 0:
            p3.t("Of the movement, ")
            split_clause(discret_id, discret, "drivers that management controls")
            p3.t(" and ")
            split_clause(nondiscret_id, nondiscret, "FX translation and one-off items")
            p3.t(".")
        elif discret == 0 and nondiscret != 0:
            p3.t("The entire movement arises in FX translation and one-off items, outside management's operating decisions.")
        else:
            p3.t("The entire movement sits in drivers that management can still influence in the remaining quarter.")
    driver_ids = {d["driver"] for d in variance["drivers"]}
    outlook: list[str] = []
    if "D_TIMING" in driver_ids:
        outlook.append("The timing element is expected to reverse in November and does not change the full-year outlook")
    if "D_ONEOFF" in driver_ids:
        outlook.append("the one-off element is non-recurring and is excluded from the run-rate carried into the forecast")
    if "D_FX" in driver_ids:
        outlook.append("the FX element reflects translation only and has no cash impact at entity level")
    if outlook:
        sentence = "; ".join(outlook)
        sentence = sentence[0].upper() + sentence[1:]
        p3.t(" " + sentence + ".")
    elif "D_VOLUME" in driver_ids or "D_PRICE" in driver_ids:
        p3.t(" The volume and price elements are structural and are carried into the November forecast.")
    else:
        p3.t(" No reversal is expected in the following period.")

    return {
        "section_id": f"S-{index:02d}",
        "kind": "variance",
        "variance_id": variance["variance_id"],
        "scope": {
            "entity": variance["entity"],
            "account": account,
            "period": variance["period"],
            "comparator": "budget",
        },
        "heading": f"{variance['account_short']} — {entity_name}",
        "eyebrow": f"{variance['entity']} · {account} · {period_label} · vs budget · USD",
        "paragraphs": [p1.to_dict(), p2.to_dict(), p3.to_dict()],
        "favorability": variance["favorability"],
        "driver_fact_ids": [d["fact_id"] for d in variance["drivers"]],
    }


def build_plan(recon: dict) -> list[dict]:
    facts = {f["fact_id"]: f for f in recon["facts"]}
    agg = {a["aggregate_id"]: a for a in recon["aggregates"]}
    sections = [_summary_section(agg, facts, recon["period"])]

    material = [v for v in recon["variances"] if v["material"]]
    material.sort(key=lambda v: (v["entity"], -abs(D(v["variance_usd"]))))
    for index, variance in enumerate(material, start=2):
        sections.append(_variance_section(variance, facts, index))
    return sections


# ---------------------------------------------------------------------------
# Drift injection (adversarial test path)
# ---------------------------------------------------------------------------
DRIFT_DESCRIPTION = [
    "A figure was produced in prose with no provenance (classic hallucination).",
    "A figure span cites the prior-period variance where the budget variance belongs (wrong cell reference).",
]


def _apply_drift(sections: list[dict], recon: dict, facts: dict[str, dict]) -> dict:
    """Inject two realistic model-drift defects into the largest variance section."""
    targets = [s for s in sections if s["kind"] == "variance"]
    if not targets:
        return {"applied": False, "section_id": None, "mutations": []}
    by_var = {v["variance_id"]: v for v in recon["variances"]}
    targets.sort(key=lambda s: -abs(D(by_var[s["variance_id"]]["variance_usd"])))
    section = targets[0]
    variance = by_var[section["variance_id"]]

    # Defect 1: an unsourced figure appended to the position paragraph.
    section["paragraphs"][0]["tokens"].append({
        "t": "text",
        "v": " A further $40K of accrued spend is expected to unwind in November.",
    })

    # Defect 2: the attribution paragraph cites the prior-period variance fact
    # where a driver fact belongs.
    prior_fact_id = variance["fact_ids"]["variance_prior"]
    para = section["paragraphs"][1]
    swapped = False
    for token in para["tokens"]:
        if token["t"] == "fig" and token["fact"] in section["driver_fact_ids"]:
            token["fact"] = prior_fact_id
            swapped = True
            break

    return {
        "applied": True,
        "section_id": section["section_id"],
        "variance_id": section["variance_id"],
        "mutations": DRIFT_DESCRIPTION,
        "second_defect_injected": swapped,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def narrate(recon: dict, mode: str = "strict") -> dict:
    mode = mode if mode in MODE_LABELS else "strict"
    sections = build_plan(recon)
    facts = {f["fact_id"]: f for f in recon["facts"]}
    drift = {"applied": False, "section_id": None, "mutations": []}
    if mode == "drift":
        drift = _apply_drift(sections, recon, facts)

    figure_total = sum(
        1 for s in sections for p in s["paragraphs"] for t in p["tokens"] if t["t"] == "fig"
    )
    word_total = sum(
        len(t["v"].split()) for s in sections for p in s["paragraphs"] for t in p["tokens"] if t["t"] == "text"
    )

    reviewed = [
        {
            "variance_id": v["variance_id"],
            "entity": v["entity"],
            "account": v["account"],
            "account_short": v["account_short"],
            "variance_usd": v["variance_usd"],
            "variance_pct": v["variance_pct"],
            "materiality_reason": v["materiality_reason"],
        }
        for v in recon["variances"]
        if not v["material"]
    ]

    return {
        "stage": "narrate",
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "generated_by": "close-engine narrator v1 (fact-constrained)",
        "sections": sections,
        "reviewed_no_commentary": reviewed,
        "drift": drift,
        "stats": {
            "sections": len(sections),
            "variance_sections": len([s for s in sections if s["kind"] == "variance"]),
            "figures": figure_total,
            "words": word_total,
            "reviewed_accounts": len(reviewed),
        },
    }
