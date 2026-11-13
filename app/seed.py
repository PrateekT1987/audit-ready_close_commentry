"""Deterministic generation of the close dataset.

Produces `data/dataset.json`: a self-contained, realistic multi-entity close
pack containing journal-line detail (the ledger), the plan of record (budget
and forecast), FX rates, driver attribution tables, intercompany pairs and a
subledger extract used for tie-out.

Everything is seeded and reproducible. The dataset intentionally contains two
immaterial exceptions (an intercompany cut-off difference and a cash subledger
break) so that the attestation step has something real to attest to.
"""
from __future__ import annotations

import json
import random
from decimal import Decimal

from app import config
from app.config import ACCOUNTS, ENTITIES, PERIODS
from app.money import D, q2

SEED = 20261031

# Month-on-month scaling of driver magnitudes and plan.
DRIVER_FACTOR = {"2026-07": Decimal("0.45"), "2026-08": Decimal("0.62"), "2026-09": Decimal("0.78"), "2026-10": Decimal("1.00")}
PLAN_FACTOR = {"2026-07": Decimal("0.94"), "2026-08": Decimal("0.965"), "2026-09": Decimal("0.99"), "2026-10": Decimal("1.00")}
BS_PATH = {"2026-06": Decimal("0.82"), "2026-07": Decimal("0.86"), "2026-08": Decimal("0.905"), "2026-09": Decimal("0.952"), "2026-10": Decimal("1.00")}

# ---------------------------------------------------------------------------
# Plan of record — October 2026 budget by entity and account, local currency
# ---------------------------------------------------------------------------
BASE_BUDGET: dict[str, dict[str, int]] = {
    "US01": {
        "4000": 4_200_000, "4100": 85_000, "5000": 1_680_000, "6000": 1_240_000,
        "6100": 1_050_000, "6200": 620_000, "6300": 310_000, "6400": 95_000,
        "6500": 48_000, "7000": 140_000, "8000": 22_000, "9000": 180_000,
    },
    "DE02": {
        "4000": 2_850_000, "4100": 41_000, "5000": 1_190_000, "6000": 985_000,
        "6100": 640_000, "6200": 410_000, "6300": 205_000, "6400": 48_000,
        "6500": 36_000, "7000": 96_000, "8000": 15_000, "9000": 118_000,
    },
    "SG03": {
        "4000": 1_420_000, "4100": 22_000, "5000": 620_000, "6000": 540_000,
        "6100": 285_000, "6200": 190_000, "6300": 120_000, "6400": 34_000,
        "6500": 22_000, "7000": 52_000, "8000": 9_000, "9000": 62_000,
    },
    "UK04": {
        "4000": 1_180_000, "4100": 14_000, "5000": 500_000, "6000": 470_000,
        "6100": 215_000, "6200": 130_000, "6300": 96_000, "6400": 28_000,
        "6500": 18_000, "7000": 41_000, "8000": 7_000, "9000": 48_000,
    },
}

# Balance-sheet closing balances at 2026-10, local currency.
BASE_BS: dict[str, dict[str, int]] = {
    "US01": {"1000": 2_140_000, "1100": 3_910_000, "1200": 612_000, "1300": 780_000,
             "2000": 1_480_000, "2100": 940_000, "2200": 0, "3000": 1_260_000},
    "DE02": {"1000": 1_090_000, "1100": 2_260_000, "1200": 318_000, "1300": 210_000,
             "2000": 860_000, "2100": 520_000, "2200": 742_000, "3000": 690_000},
    "SG03": {"1000": 780_000, "1100": 1_120_000, "1200": 145_000, "1300": 185_000,
             "2000": 410_000, "2100": 260_000, "2200": 0, "3000": 340_000},
    "UK04": {"1000": 520_000, "1100": 840_000, "1200": 0, "1300": 0,
             "2000": 330_000, "2100": 190_000, "2200": 114_000, "3000": 210_000},
}

# ---------------------------------------------------------------------------
# Driver attribution — the "why" table maintained by FP&A.
# (driver_id, October amount in local currency, noun phrase describing the cause)
# ---------------------------------------------------------------------------
DRIVER_TABLE: dict[tuple[str, str], list[tuple[str, int, str]]] = {
    ("US01", "4000"): [
        ("D_VOLUME", 168_000, "enterprise seat expansion across the top twenty accounts"),
        ("D_PRICE", 42_000, "the annual list-price uplift"),
        ("D_FX", -9_500, "translation of USD-denominated contracts at a stronger dollar"),
    ],
    ("US01", "5000"): [
        ("D_VOLUME", 74_000, "higher hosting volume supporting the revenue growth"),
        ("D_SUPPLIER", 31_000, "the cloud infrastructure supplier price increase effective October"),
        ("D_MIX", -12_000, "a favourable mix shift toward higher-margin self-serve plans"),
    ],
    ("US01", "6000"): [
        ("D_HEADCOUNT", 58_000, "net twenty-two hires in engineering and customer success"),
        ("D_COMP", 14_500, "the mid-year merit cycle annualising into the run-rate"),
    ],
    ("US01", "6100"): [
        ("D_MARKETING", 210_000, "the Q4 brand campaign launched in the second week of October"),
        ("D_TIMING", -24_000, "two regional field events moving into November"),
    ],
    ("US01", "6200"): [
        ("D_HEADCOUNT", 18_000, "the platform research team build-out"),
        ("D_TIMING", -6_000, "contractor invoices received after the cut-off"),
    ],
    ("US01", "6300"): [
        ("D_COMP", 9_000, "merit annualisation in the shared-services function"),
        ("D_OTHER", 4_000, "higher facilities and insurance costs"),
    ],
    ("US01", "6400"): [
        ("D_ONEOFF", 38_000, "the employment matter settled in October"),
        ("D_TIMING", -4_000, "the annual audit fee billed ahead of schedule"),
    ],
    ("US01", "6500"): [
        ("D_POLICY", -9_500, "the travel approval policy tightened from 1 October"),
        ("D_VOLUME", 2_000, "additional customer visits in the quarter"),
    ],
    ("US01", "1100"): [
        ("D_VOLUME", 96_000, "revenue growth lifting billed receivables"),
        ("D_TIMING", 54_000, "slower collections from two enterprise customers"),
    ],
    ("DE02", "4000"): [
        ("D_VOLUME", 96_000, "expansion in the DACH mid-market segment"),
        ("D_PRICE", 34_000, "the indexed price adjustment on renewals"),
        ("D_FX", -21_000, "translation of euro-denominated sales at a weaker closing rate"),
    ],
    ("DE02", "5000"): [
        ("D_SUPPLIER", 48_000, "the renegotiated data-centre contract"),
        ("D_VOLUME", 21_000, "higher delivery volume in the DACH region"),
    ],
    ("DE02", "6000"): [("D_HEADCOUNT", 12_000, "six net additions in the delivery organisation")],
    ("DE02", "6100"): [
        ("D_MARKETING", 41_000, "the industry conference sponsorship"),
        ("D_ONEOFF", 22_000, "the product launch event in Munich"),
        ("D_TIMING", -8_000, "the partner programme invoice shifting to November"),
    ],
    ("DE02", "6200"): [("D_HEADCOUNT", 26_000, "the localisation engineering team")],
    ("DE02", "6300"): [("D_COMP", 6_500, "merit annualisation in the German entity")],
    ("DE02", "6400"): [("D_ONEOFF", 9_400, "the trade-mark filing programme")],
    ("DE02", "6500"): [("D_POLICY", -5_200, "the revised travel policy")],
    ("DE02", "1100"): [("D_VOLUME", 18_000, "higher DACH billings")],
    ("SG03", "4000"): [
        ("D_VOLUME", 118_000, "the APAC enterprise pipeline converting in October"),
        ("D_PRICE", 18_000, "the annual price uplift on the ANZ renewal cohort"),
        ("D_MIX", 22_000, "a higher share of multi-year platform contracts"),
    ],
    ("SG03", "5000"): [
        ("D_SUPPLIER", 33_000, "the regional hosting provider repricing"),
        ("D_VOLUME", 18_000, "higher delivery volume across APAC"),
    ],
    ("SG03", "6000"): [
        ("D_HEADCOUNT", 31_000, "fourteen net hires in the Singapore hub"),
        ("D_COMP", 8_000, "the local merit cycle"),
    ],
    ("SG03", "6100"): [("D_MARKETING", 26_000, "the APAC digital demand-generation programme")],
    ("SG03", "6200"): [("D_HEADCOUNT", 17_000, "the Singapore engineering centre")],
    ("SG03", "6300"): [("D_TIMING", 14_000, "the statutory audit fee brought forward from November")],
    ("SG03", "6400"): [("D_ONEOFF", 6_000, "the regional entity restructuring advice")],
    ("SG03", "6500"): [("D_POLICY", -3_000, "the travel policy revision")],
    ("SG03", "1100"): [("D_VOLUME", 22_000, "higher APAC billings")],
    ("UK04", "4000"): [
        ("D_VOLUME", -42_000, "the public-sector contract ending in September"),
        ("D_FX", -11_000, "translation at a weaker closing rate"),
        ("D_PRICE", 9_000, "the renewal price indexation"),
    ],
    ("UK04", "5000"): [
        ("D_SUPPLIER", 21_000, "the managed-service contract renewal"),
        ("D_VOLUME", -8_000, "lower delivery volume after the contract exit"),
    ],
    ("UK04", "6000"): [("D_HEADCOUNT", 19_000, "the seven net hires in the London office")],
    ("UK04", "6100"): [
        ("D_MARKETING", 14_000, "the UK brand refresh"),
        ("D_POLICY", -6_000, "the agency retainer consolidation"),
    ],
    ("UK04", "6200"): [("D_HEADCOUNT", 9_500, "the platform security team")],
    ("UK04", "6300"): [("D_COMP", 4_000, "merit annualisation")],
    ("UK04", "6400"): [("D_ONEOFF", 16_000, "the lease surrender negotiation")],
    ("UK04", "6500"): [("D_POLICY", -4_600, "the travel policy revision")],
    ("UK04", "1100"): [("D_TIMING", 9_000, "a large public-sector invoice unpaid at month end")],
}

# Journal-line narratives by account family, used to build believable detail.
LINE_DESCRIPTIONS: dict[str, list[str]] = {
    "revenue": ["Invoice batch", "Subscription billing run", "Usage overage invoicing", "Credit memo issued", "Deferred revenue release"],
    "cogs": ["Hosting invoice", "Third-party delivery costs", "Support cost allocation", "Data egress charges"],
    "opex": ["Payroll journal", "Accrual release", "Vendor invoice", "Intercompany recharge", "Expense report batch"],
    "other": ["Depreciation run", "Interest accrual", "Tax provision", "Amortisation schedule"],
    "asset": ["Customer receipt", "Bank activity", "Invoice posting", "Inventory movement", "Intercompany settlement"],
    "liability": ["Supplier invoice", "Accrual build", "Payment run", "Deferred revenue movement", "Intercompany settlement"],
}

DOC_PREFIX = {"revenue": "INV", "cogs": "VND", "opex": "VND", "other": "JRN", "asset": "RCP", "liability": "AP"}

# Account-level scaling applied to both the plan of record and the driver
# table, so the group lands at a credible margin without disturbing the
# variance percentages the commentary is built around.
ACCOUNT_SCALE = {"revenue": Decimal("1.45"), "cogs": Decimal("1.45"), "opex": Decimal("0.85")}


def _scaled(account_code: str, amount) -> Decimal:
    cls = ACCOUNTS[account_code].cls
    return D(amount) * ACCOUNT_SCALE.get(cls, Decimal("1"))


def _journal_split(total: Decimal, rng: random.Random, parts: int) -> list[Decimal]:
    """Split a signed total into `parts` non-zero parts summing exactly."""
    if total == 0:
        return []
    weights = [Decimal(rng.randint(60, 140)) for _ in range(parts)]
    weight_sum = sum(weights)
    pieces = [q2(total * w / weight_sum) for w in weights]
    drift = q2(total - sum(pieces))
    pieces[-1] = q2(pieces[-1] + drift)
    return [p for p in pieces if p != 0]


def build_dataset() -> dict:
    rng = random.Random(SEED)
    dataset: dict = {
        "meta": {
            "group": config.GROUP_NAME,
            "group_currency": config.GROUP_CURRENCY,
            "current_period": config.CURRENT_PERIOD,
            "prior_period": config.PRIOR_PERIOD,
            "periods": PERIODS,
            "seed": SEED,
            "prepared_by": "close-engine v1",
            "source_systems": {
                "ledger": "NetSuite OneWorld (GL extract, debit-positive)",
                "plan": "Anaplan FY26 forecast cycle 10",
                "fx": "Group treasury month-end closing rates",
                "subledger": "Cash subledger extract, 2026-10-31",
            },
        },
        "entities": [e.__dict__.copy() for e in ENTITIES],
        "accounts": [a.to_dict() for a in config.ACCOUNT_LIST],
        "fx": {p: {c: str(r) for c, r in rates.items()} for p, rates in config.FX_RATES.items()},
        "journal": [],
        "plan": [],
        "drivers": [],
        "bs_closing": [],
        "subledger": [],
        "intercompany": [],
        "exceptions": [],
    }

    je_seq = 1000
    per_counter: dict[str, int] = {}

    def entity_currency(code: str) -> str:
        return config.ENTITY_BY_CODE[code].currency

    # ---- Plan (budget + forecast) for every entity/account/period ----------
    drivers_by_key = DRIVER_TABLE
    plan_rows: dict[tuple[str, str, str], dict] = {}

    def driver_total_local(code: str, account_code: str, period: str) -> Decimal:
        return sum(
            (_scaled(account_code, amount) * DRIVER_FACTOR[period]
             for _, amount, _ in drivers_by_key.get((code, account_code), [])),
            D(0),
        )

    for entity in ENTITIES:
        code, cur = entity.code, entity.currency
        for account_code, base in BASE_BUDGET[code].items():
            account = ACCOUNTS[account_code]
            for period in PERIODS:
                budget = (_scaled(account_code, base) * PLAN_FACTOR[period]).quantize(Decimal("1"))
                driver_total = driver_total_local(code, account_code, period)
                forecast = q2(budget + driver_total * Decimal("0.40"))
                plan_rows[(code, account_code, period)] = {
                    "entity": code,
                    "account": account_code,
                    "period": period,
                    "currency": cur,
                    "budget_local": str(budget),
                    "forecast_local": str(forecast),
                }

    # ---- Balance sheet: closing balances per period ------------------------
    closing: dict[tuple[str, str, str], Decimal] = {}
    for entity in ENTITIES:
        code, cur = entity.code, entity.currency
        for account_code, base in BASE_BS[code].items():
            for period in PERIODS + ["2026-06"]:
                noise = Decimal(str(1 + rng.uniform(-0.008, 0.008)))
                value = (D(base) * BS_PATH[period] * noise).quantize(Decimal("1"))
                closing[(code, account_code, period)] = value

    # Overwrite the closed periods so the current month reconciles exactly to
    # the stated base balances (no noise on October).
    for entity in ENTITIES:
        code = entity.code
        for account_code, base in BASE_BS[code].items():
            closing[(code, account_code, "2026-10")] = D(base)

    # Budget for balance-sheet accounts = closing less the driver total, so the
    # variance is fully attributed the same way P&L variances are. All plan and
    # fact values are in presentation units (natural sign: assets, liabilities,
    # revenue and expense all shown positive).
    for entity in ENTITIES:
        code = entity.code
        for account_code in BASE_BS[code].keys():
            for period in PERIODS:
                budget_presented = closing[(code, account_code, period)] - driver_total_local(code, account_code, period)
                driver_total = closing[(code, account_code, period)] - budget_presented
                plan_rows[(code, account_code, period)] = {
                    "entity": code,
                    "account": account_code,
                    "period": period,
                    "currency": entity.currency,
                    "budget_local": str(q2(budget_presented)),
                    "forecast_local": str(q2(budget_presented + driver_total * Decimal("0.40"))),
                }

    dataset["plan"] = [plan_rows[k] for k in sorted(plan_rows)]

    # ---- Journal lines ----------------------------------------------------
    def add_lines(entity: str, account_code: str, period: str, ledger_total: Decimal) -> None:
        nonlocal je_seq
        if ledger_total == 0:
            return
        account = ACCOUNTS[account_code]
        parts = 4 if abs(ledger_total) > 50_000 else 3
        pieces = _journal_split(ledger_total, rng, parts)
        descriptions = LINE_DESCRIPTIONS[account.cls]
        for index, amount in enumerate(pieces):
            je_seq += 1
            day = 4 + (index * 6) % 24
            per_counter[entity] = per_counter.get(entity, 0) + 1
            doc = f"{DOC_PREFIX[account.cls]}-{period.replace('-', '')}-{per_counter[entity]:04d}"
            dataset["journal"].append({
                "je_id": f"JE-{je_seq}",
                "entity": entity,
                "account": account_code,
                "period": period,
                "posting_date": f"{period}-{day:02d}",
                "doc_ref": doc,
                "description": descriptions[index % len(descriptions)],
                "currency": entity_currency(entity),
                "debit": str(q2(amount)) if amount > 0 else "0.00",
                "credit": str(q2(-amount)) if amount < 0 else "0.00",
                "amount": str(q2(amount)),
            })

    for entity in ENTITIES:
        code = entity.code
        for period in PERIODS:
            for account_code in BASE_BUDGET[code]:
                account = ACCOUNTS[account_code]
                budget = D(plan_rows[(code, account_code, period)]["budget_local"])
                driver_total = driver_total_local(code, account_code, period)
                presented_actual = budget + driver_total
                # Revenue is credit-normal: ledger sum is the negative of the
                # presented amount.
                ledger_total = presented_actual * account.presentation_sign
                add_lines(code, account_code, period, q2(ledger_total))

            for account_code in BASE_BS[code]:
                account = ACCOUNTS[account_code]
                prior = "2026-06" if period == PERIODS[0] else PERIODS[PERIODS.index(period) - 1]
                if period == PERIODS[0]:
                    opening = closing[(code, account_code, "2026-06")]
                    dataset["bs_closing"].append({
                        "entity": code, "account": account_code, "period": "2026-06",
                        "closing_local": str(q2(opening)), "opening_basis": "brought forward",
                    })
                # Presentation movement -> ledger movement (debit-positive).
                pres_movement = closing[(code, account_code, period)] - closing[(code, account_code, prior)]
                ledger_movement = pres_movement * account.presentation_sign
                add_lines(code, account_code, period, q2(ledger_movement))
                dataset["bs_closing"].append({
                    "entity": code, "account": account_code, "period": period,
                    "closing_local": str(q2(closing[(code, account_code, period)])),
                })

    # ---- Driver attribution rows -----------------------------------------
    for (code, account_code), entries in drivers_by_key.items():
        for period in PERIODS:
            for driver_id, amount, phrase in entries:
                scaled = q2(_scaled(account_code, amount) * DRIVER_FACTOR[period])
                if scaled == 0:
                    continue
                dataset["drivers"].append({
                    "entity": code,
                    "account": account_code,
                    "period": period,
                    "driver": driver_id,
                    "amount_local": str(scaled),
                    "cause": phrase,
                })

    # ---- Subledger extract (cash) ----------------------------------------
    # Deliberately one immaterial break: SG03 cash posting lag at month end.
    for entity in ENTITIES:
        code = entity.code
        for account_code in ("1000",):
            gl = closing[(code, account_code, "2026-10")]
            if code == "SG03":
                gl = gl  # reported below with a break
            sub = gl
            if code == "SG03":
                sub = gl - D("9836")
            dataset["subledger"].append({
                "entity": code,
                "account": account_code,
                "period": "2026-10",
                "gl_closing_local": str(q2(gl)),
                "subledger_closing_local": str(q2(sub)),
                "extract": "cash subledger, statement of account",
            })

    # ---- Intercompany pairs ----------------------------------------------
    ic_pairs = [
        {
            "pair_id": "IC-01", "lender": "US01", "borrower": "DE02",
            "receivable_local": "780000", "payable_local": "742000",
            "purpose": "Shared services and platform recharge",
            "note": "Cut-off difference on the October service invoice; the borrower posted settlement on 3 November, after its close.",
        },
        {
            "pair_id": "IC-02", "lender": "SG03", "borrower": "UK04",
            "receivable_local": "185000", "payable_local": "114000",
            "purpose": "Regional reseller margin recharge",
            "note": "FX revaluation difference on the GBP leg recorded by the borrower at the September rate.",
        },
    ]
    for pair in ic_pairs:
        for period in PERIODS:
            scale = DRIVER_FACTOR[period] if period != "2026-10" else Decimal("1")
            dataset["intercompany"].append({
                **pair,
                "period": period,
                "receivable_local": str(q2(D(pair["receivable_local"]) * scale)),
                "payable_local": str(q2(D(pair["payable_local"]) * scale)),
                "receivable_currency": entity_currency(pair["lender"]),
                "payable_currency": entity_currency(pair["borrower"]),
            })

    dataset["exceptions"] = [
        {
            "exception_id": "EXC-01",
            "kind": "intercompany_difference",
            "period": "2026-10",
            "description": "Intercompany receivables and payables do not fully eliminate across IC-01 and IC-02.",
            "owner": "Group Financial Control",
            "status": "open_disclosed",
        },
        {
            "exception_id": "EXC-02",
            "kind": "subledger_tie_out",
            "period": "2026-10",
            "description": "Cash subledger extract for SG03 trails the general ledger by a posting lag.",
            "owner": "APAC Controller",
            "status": "open_disclosed",
        },
    ]

    return dataset


def write_dataset(path: str = "data/dataset.json") -> dict:
    dataset = build_dataset()
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dataset, handle, indent=1)
    return dataset


if __name__ == "__main__":
    data = write_dataset()
    print(f"entities: {len(data['entities'])}  journal lines: {len(data['journal'])}  plan rows: {len(data['plan'])}")
