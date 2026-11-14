"""Stage 1 — Reconcile.

Turns raw journal lines, the plan of record and the FX table into:

  * a fact store — every figure the commentary is allowed to cite, each with a
    machine-checkable derivation back to the ledger
  * a variance queue — actual vs budget / prior period / forecast, scored
    against the materiality policy
  * control results — roll-forward ties, subledger tie-outs, intercompany
    elimination, plan and FX completeness

Nothing in this module talks to a language model. It is the source of truth
that stage 2 narrates and stage 3 re-derives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app import config
from app.config import ACCOUNTS, ENTITY_BY_CODE, MATERIALITY
from app.money import D, q2, pct

US = "USD"


# ---------------------------------------------------------------------------
# Fact store
# ---------------------------------------------------------------------------
@dataclass
class Fact:
    fact_id: str
    kind: str
    label: str
    value: Decimal
    unit: str
    period: str
    basis: str
    entity: str
    account: str | None
    derivation: dict[str, Any]
    source_system: str
    source_ref: str
    comparator: str | None = None
    aggregate: str | None = None

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind,
            "label": self.label,
            "value": str(self.value),
            "unit": self.unit,
            "currency": US if self.unit == "currency" else None,
            "period": self.period,
            "basis": self.basis,
            "entity": self.entity,
            "account": self.account,
            "comparator": self.comparator,
            "aggregate": self.aggregate,
            "derivation": self.derivation,
            "source": {"system": self.source_system, "ref": self.source_ref},
        }


class FactStore:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._seq = 0

    def add(self, **kwargs) -> Fact:
        self._seq += 1
        fact = Fact(fact_id=f"F-{self._seq:04d}", **kwargs)
        self._facts[fact.fact_id] = fact
        return fact

    def get(self, fact_id: str) -> Fact | None:
        return self._facts.get(fact_id)

    def all(self) -> list[Fact]:
        return list(self._facts.values())

    def to_list(self) -> list[dict]:
        return [f.to_dict() for f in self._facts.values()]

    def __len__(self) -> int:
        return len(self._facts)


# ---------------------------------------------------------------------------
# Ledger access with Decimal discipline
# ---------------------------------------------------------------------------
class Ledger:
    def __init__(self, dataset: dict) -> None:
        self.dataset = dataset
        self.periods: list[str] = dataset["meta"]["periods"]
        self.current: str = dataset["meta"]["current_period"]
        self.prior: str = dataset["meta"]["prior_period"]
        self.entities = {e["code"]: e for e in dataset["entities"]}
        self.fx = {p: {c: D(r) for c, r in rates.items()} for p, rates in dataset["fx"].items()}
        self._journal: dict[tuple[str, str, str], list[dict]] = {}
        for line in dataset["journal"]:
            key = (line["entity"], line["account"], line["period"])
            self._journal.setdefault(key, []).append(line)
        self._plan = {(r["entity"], r["account"], r["period"]): r for r in dataset["plan"]}
        self._closing = {(r["entity"], r["account"], r["period"]): D(r["closing_local"]) for r in dataset["bs_closing"]}
        self._drivers: dict[tuple[str, str, str], list[dict]] = {}
        for row in dataset["drivers"]:
            key = (row["entity"], row["account"], row["period"])
            self._drivers.setdefault(key, []).append(row)
        self._subledger = {(r["entity"], r["account"], r["period"]): r for r in dataset["subledger"]}

    # -- raw ---------------------------------------------------------------
    def currency(self, entity: str) -> str:
        return self.entities[entity]["currency"]

    def rate(self, entity: str, period: str) -> Decimal:
        return self.fx[period][self.currency(entity)]

    def lines(self, entity: str, account: str, period: str) -> list[dict]:
        return self._journal.get((entity, account, period), [])

    def ledger_sum(self, entity: str, account: str, period: str) -> Decimal:
        return sum((D(line["amount"]) for line in self.lines(entity, account, period)), D(0))

    # -- presentation ------------------------------------------------------
    def presented_local(self, entity: str, account: str, period: str) -> Decimal:
        account_meta = ACCOUNTS[account]
        return q2(account_meta.presentation_sign * self.ledger_sum(entity, account, period))

    def presented_usd(self, entity: str, account: str, period: str) -> Decimal:
        return q2(self.presented_local(entity, account, period) * self.rate(entity, period))

    def presented_actual(self, entity: str, account: str, period: str) -> Decimal:
        """Reported balance for the account in presentation units.

        Balance-sheet accounts report a closing balance; P&L accounts report the
        period flow. Keeping this distinction in one place stops the engine from
        comparing a closing balance against a monthly budget.
        """
        meta = ACCOUNTS[account]
        if meta.statement == "BS":
            closing = self.closing_local(entity, account, period)
            return q2(closing) if closing is not None else D(0)
        return self.presented_local(entity, account, period)

    def presented_actual_usd(self, entity: str, account: str, period: str) -> Decimal:
        return q2(self.presented_actual(entity, account, period) * self.rate(entity, period))

    def closing_local(self, entity: str, account: str, period: str) -> Decimal | None:
        return self._closing.get((entity, account, period))

    def plan_local(self, entity: str, account: str, period: str, basis: str) -> Decimal:
        row = self._plan.get((entity, account, period))
        if row is None:
            return D(0)
        return D(row[f"{basis}_local"])

    def plan_usd(self, entity: str, account: str, period: str, basis: str) -> Decimal:
        return q2(self.plan_local(entity, account, period, basis) * self.rate(entity, period))

    def drivers(self, entity: str, account: str, period: str) -> list[dict]:
        return self._drivers.get((entity, account, period), [])

    def driver_amount_usd(self, row: dict) -> Decimal:
        return q2(D(row["amount_local"]) * self.rate(row["entity"], row["period"]))

    def subledger_row(self, entity: str, account: str, period: str) -> dict | None:
        return self._subledger.get((entity, account, period))


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
COMPARATORS = [
    ("budget", "Budget", "budget"),
    ("prior", "Prior period", "actual"),
    ("forecast", "Forecast", "forecast"),
]


def _materiality_policy(account: str) -> dict:
    return MATERIALITY[ACCOUNTS[account].statement]


def _is_material(variance_usd: Decimal, variance_pct: Decimal, account: str) -> tuple[bool, str]:
    policy = _materiality_policy(account)
    amount_ok = abs(variance_usd) >= policy["amount_usd"]
    pct_ok = abs(variance_pct) >= policy["min_pct"]
    if amount_ok and pct_ok:
        return True, "clears both the amount and percentage floors"
    if not amount_ok and not pct_ok:
        return False, "below both the amount and percentage floors"
    if not amount_ok:
        return False, f"below the {policy['amount_usd']:,.0f} USD amount floor"
    return False, f"below the {policy['min_pct'] * 100:.0f}% relative floor"


def _favorability(variance_usd: Decimal, account: str) -> str:
    meta = ACCOUNTS[account]
    if meta.favorability == "neutral" or variance_usd == 0:
        return "neutral"
    if meta.favorability == "higher_better":
        return "favorable" if variance_usd > 0 else "unfavorable"
    return "unfavorable" if variance_usd > 0 else "favorable"


def _direction(variance_usd: Decimal) -> str:
    if variance_usd > 0:
        return "increase"
    if variance_usd < 0:
        return "decrease"
    return "flat"


def _decomposition(ledger: Ledger, entity: str, account: str, comparator: str) -> list[dict]:
    """Attribute a variance to named causes.

    For the budget comparator the drivers for the current month are the
    explanation by construction. For the prior-period comparator the items are
    the plan step plus the month-on-month change in each driver, which sums to
    the same variance.
    """
    period = ledger.current
    current_rows = {r["driver"]: r for r in ledger.drivers(entity, account, period)}
    if comparator == "budget":
        items = [
            {
                "driver": driver_id,
                "label": config.DRIVERS.get(driver_id, driver_id),
                "cause": row["cause"],
                "amount": str(ledger.driver_amount_usd(row)),
            }
            for driver_id, row in current_rows.items()
        ]
        items.sort(key=lambda x: abs(D(x["amount"])), reverse=True)
        return items

    prior_rows = {r["driver"]: r for r in ledger.drivers(entity, account, ledger.prior)}
    plan_step = ledger.plan_usd(entity, account, period, "budget") - ledger.plan_usd(entity, account, ledger.prior, "budget")
    items = [{
        "driver": "PLAN_STEP",
        "label": "Plan step-up",
        "cause": "the movement in the approved plan between periods",
        "amount": str(q2(plan_step)),
    }]
    for driver_id in sorted(set(current_rows) | set(prior_rows)):
        cur = ledger.driver_amount_usd(current_rows[driver_id]) if driver_id in current_rows else D(0)
        prior = ledger.driver_amount_usd(prior_rows[driver_id]) if driver_id in prior_rows else D(0)
        delta = q2(cur - prior)
        if delta == 0:
            continue
        cause = (current_rows.get(driver_id) or prior_rows.get(driver_id))["cause"]
        items.append({
            "driver": driver_id,
            "label": config.DRIVERS.get(driver_id, driver_id),
            "cause": cause,
            "amount": str(delta),
        })
    items.sort(key=lambda x: abs(D(x["amount"])), reverse=True)
    return items


def _prior_derivation(ledger: Ledger, entity: str, account: str, period: str, currency: str, meta) -> dict:
    if meta.statement == "BS":
        prev_index = ledger.periods.index(period) - 1
        opening_period = "2026-06" if prev_index < 0 else ledger.periods[prev_index]
        return {
            "type": "bs_closing", "entity": entity, "account": account,
            "period": period, "opening_period": opening_period,
            "currency": currency, "rate": str(ledger.rate(entity, period)),
            "presentation_sign": meta.presentation_sign,
        }
    return {
        "type": "ledger_sum", "entity": entity, "account": account,
        "period": period, "currency": currency,
        "rate": str(ledger.rate(entity, period)),
        "presentation_sign": meta.presentation_sign,
        "lines": [{"je_id": l["je_id"], "amount": l["amount"]} for l in ledger.lines(entity, account, period)],
    }


def _tb_columns(presented: Decimal, presentation_sign: int) -> tuple[Decimal, Decimal]:
    """Split a presented balance into its debit and credit columns.

    A positive balance sits on the account's normal side. A contra balance
    (negative cash, a debit balance in deferred revenue) flips to the other
    column, which is what makes the trial balance self-proving.
    """
    if presented >= 0:
        return (presented, D(0)) if presentation_sign > 0 else (D(0), presented)
    return (D(0), -presented) if presentation_sign > 0 else (-presented, D(0))


def reconcile(dataset: dict) -> dict:
    ledger = Ledger(dataset)
    facts = FactStore()
    period, prior_period = ledger.current, ledger.prior

    # ---- 1. Trial balance with FX translation ----------------------------
    trial_balance: list[dict] = []
    for entity in dataset["entities"]:
        code = entity["code"]
        for account_code in [a.code for a in config.ACCOUNT_LIST]:
            lines = ledger.lines(code, account_code, period)
            if not lines and ledger.plan_local(code, account_code, period, "budget") == 0:
                continue
            presented_local = ledger.presented_actual(code, account_code, period)
            rate = ledger.rate(code, period)
            debit_local, credit_local = _tb_columns(presented_local, ACCOUNTS[account_code].presentation_sign)
            trial_balance.append({
                "entity": code,
                "entity_name": entity["name"],
                "account": account_code,
                "account_name": ACCOUNTS[account_code].name,
                "statement": ACCOUNTS[account_code].statement,
                "currency": entity["currency"],
                "rate": str(rate),
                "line_count": len(lines),
                "presented_local": str(presented_local),
                "presented_usd": str(q2(presented_local * rate)),
                # Unrounded USD columns keep the control exact: each entity's
                # local debits and credits are equal to the cent, so their
                # translated totals are equal too.
                "debit_local": str(q2(debit_local)),
                "credit_local": str(q2(credit_local)),
                "debit_usd": str(debit_local * rate),
                "credit_usd": str(credit_local * rate),
            })

        # Brought-forward equity: prior-period net assets. The current-period
        # P&L is still open in the trial balance, so equity is the opening
        # balance and assets - liabilities = equity + current result.
        assets_prior = sum(
            (ledger.closing_local(code, a.code, prior_period) or D(0)
             for a in config.ACCOUNT_LIST if a.statement == "BS" and a.cls == "asset"),
            D(0),
        )
        liabilities_prior = sum(
            (ledger.closing_local(code, a.code, prior_period) or D(0)
             for a in config.ACCOUNT_LIST if a.statement == "BS" and a.cls == "liability"),
            D(0),
        )
        retained_local = q2(assets_prior - liabilities_prior)
        rate = ledger.rate(code, period)
        equity = config.RETAINED_EARNINGS
        debit_local, credit_local = _tb_columns(retained_local, equity.presentation_sign)
        trial_balance.append({
            "entity": code,
            "entity_name": entity["name"],
            "account": equity.code,
            "account_name": equity.name,
            "statement": equity.statement,
            "currency": entity["currency"],
            "rate": str(rate),
            "line_count": 0,
            "presented_local": str(retained_local),
            "presented_usd": str(q2(retained_local * rate)),
            "debit_local": str(q2(debit_local)),
            "credit_local": str(q2(credit_local)),
            "debit_usd": str(debit_local * rate),
            "credit_usd": str(credit_local * rate),
        })

    # ---- 2. Facts for every actual and plan figure -----------------------
    actual_facts: dict[tuple[str, str], str] = {}
    plan_facts: dict[tuple[str, str, str], str] = {}
    for entity in dataset["entities"]:
        code = entity["code"]
        for account_code in [a.code for a in config.ACCOUNT_LIST]:
            has_activity = bool(ledger.lines(code, account_code, period))
            has_plan = ledger.plan_local(code, account_code, period, "budget") != 0
            if not (has_activity or has_plan):
                continue
            meta = ACCOUNTS[account_code]
            actual = ledger.presented_actual_usd(code, account_code, period)
            if ACCOUNTS[account_code].statement == "BS":
                prev_index = ledger.periods.index(period) - 1
                opening_period = "2026-06" if prev_index < 0 else ledger.periods[prev_index]
                derivation = {
                    "type": "bs_closing", "entity": code, "account": account_code,
                    "period": period, "opening_period": opening_period,
                    "currency": entity["currency"], "rate": str(ledger.rate(code, period)),
                    "presentation_sign": meta.presentation_sign,
                }
            else:
                derivation = {
                    "type": "ledger_sum",
                    "entity": code,
                    "account": account_code,
                    "period": period,
                    "currency": entity["currency"],
                    "rate": str(ledger.rate(code, period)),
                    "presentation_sign": meta.presentation_sign,
                    "lines": [{"je_id": l["je_id"], "amount": l["amount"]} for l in ledger.lines(code, account_code, period)],
                }
            fact = facts.add(
                kind="actual",
                label=f"Actual {meta.name}, {entity['name']}, {config.PERIOD_LABELS[period]}",
                value=actual,
                unit="currency",
                period=period,
                basis="actual",
                entity=code,
                account=account_code,
                derivation=derivation,
                source_system=dataset["meta"]["source_systems"]["ledger"],
                source_ref=f"{code}/{account_code}/{period}, {len(ledger.lines(code, account_code, period))} journal lines",
            )
            actual_facts[(code, account_code)] = fact.fact_id

            for basis_key, basis_label in (("budget", "Budget"), ("forecast", "Forecast")):
                plan_value = ledger.plan_usd(code, account_code, period, basis_key)
                plan_fact = facts.add(
                    kind=f"{basis_key}",
                    label=f"{basis_label} {meta.name}, {entity['name']}, {config.PERIOD_LABELS[period]}",
                    value=plan_value,
                    unit="currency",
                    period=period,
                    basis=basis_key,
                    entity=code,
                    account=account_code,
                    derivation={
                        "type": "plan_lookup",
                        "entity": code,
                        "account": account_code,
                        "period": period,
                        "basis": basis_key,
                        "currency": entity["currency"],
                        "rate": str(ledger.rate(code, period)),
                    },
                    source_system=dataset["meta"]["source_systems"]["plan"],
                    source_ref=f"{code}/{account_code}/{period} {basis_key}",
                )
                plan_facts[(code, account_code, basis_key)] = plan_fact.fact_id

    # ---- 3. Variances ----------------------------------------------------
    variances: list[dict] = []
    variance_seq = 0
    for entity in dataset["entities"]:
        code = entity["code"]
        for account_code in [a.code for a in config.ACCOUNT_LIST]:
            if (code, account_code) not in actual_facts:
                continue
            meta = ACCOUNTS[account_code]
            actual_usd = ledger.presented_actual_usd(code, account_code, period)
            budget_usd = ledger.plan_usd(code, account_code, period, "budget")
            prior_usd = ledger.presented_actual_usd(code, account_code, prior_period)
            forecast_usd = ledger.plan_usd(code, account_code, period, "forecast")

            variance_usd = q2(actual_usd - budget_usd)
            variance_pct = pct(variance_usd, abs(budget_usd)) if budget_usd != 0 else D(0)
            material, reason = _is_material(variance_usd, variance_pct, account_code)
            policy = _materiality_policy(account_code)

            variance_seq += 1
            vid = f"V-{variance_seq:03d}"

            prior_var = q2(actual_usd - prior_usd)
            prior_pct = pct(prior_var, abs(prior_usd)) if prior_usd != 0 else D(0)
            forecast_var = q2(actual_usd - forecast_usd)

            fact_ids = {
                "actual": actual_facts[(code, account_code)],
                "budget": plan_facts[(code, account_code, "budget")],
                "prior": facts.add(
                    kind="comparator", label=f"Prior period {meta.name}, {entity['name']}",
                    value=prior_usd, unit="currency", period=prior_period, basis="actual",
                    entity=code, account=account_code,
                    derivation=_prior_derivation(ledger, code, account_code, prior_period, entity["currency"], meta),
                    source_system=dataset["meta"]["source_systems"]["ledger"],
                    source_ref=f"{code}/{account_code}/{prior_period}",
                ).fact_id,
            }

            var_fact = facts.add(
                kind="variance",
                label=f"Variance vs budget, {meta.name}, {entity['name']}, {config.PERIOD_LABELS[period]}",
                value=variance_usd, unit="currency", period=period, basis="variance",
                entity=code, account=account_code,
                derivation={"type": "difference", "left": fact_ids["actual"], "right": fact_ids["budget"]},
                source_system="close-engine (derived)",
                source_ref=f"actual less budget for {code}/{account_code}/{period}",
                comparator="budget",
            )
            fact_ids["variance"] = var_fact.fact_id

            pct_fact = facts.add(
                kind="variance_pct",
                label=f"Variance vs budget %, {meta.name}, {entity['name']}",
                value=variance_pct, unit="pct", period=period, basis="variance",
                entity=code, account=account_code,
                derivation={"type": "ratio", "numerator": var_fact.fact_id, "denominator": fact_ids["budget"]},
                source_system="close-engine (derived)",
                source_ref=f"variance over budget base for {code}/{account_code}",
                comparator="budget",
            )
            fact_ids["variance_pct"] = pct_fact.fact_id

            prior_fact = facts.add(
                kind="variance_prior",
                label=f"Variance vs prior period, {meta.name}, {entity['name']}",
                value=prior_var, unit="currency", period=period, basis="variance",
                entity=code, account=account_code,
                derivation={"type": "difference", "left": fact_ids["actual"], "right": fact_ids["prior"]},
                source_system="close-engine (derived)",
                source_ref=f"actual less prior period for {code}/{account_code}",
                comparator="prior",
            )
            fact_ids["variance_prior"] = prior_fact.fact_id

            # Driver decomposition (budget comparator) and its closure check.
            drivers = _decomposition(ledger, code, account_code, "budget")
            driver_facts = []
            for item in drivers:
                dfact = facts.add(
                    kind="driver",
                    label=f"{item['label']}: {item['cause']}, {meta.name}, {entity['name']}",
                    value=item["amount"], unit="currency", period=period, basis="driver",
                    entity=code, account=account_code,
                    derivation={
                        "type": "driver_sum",
                        "entity": code, "account": account_code, "period": period,
                        "driver": item["driver"], "currency": entity["currency"],
                        "rate": str(ledger.rate(code, period)),
                    },
                    source_system=dataset["meta"]["source_systems"]["plan"],
                    source_ref=f"driver attribution {code}/{account_code}/{period}/{item['driver']}",
                )
                driver_facts.append({**item, "fact_id": dfact.fact_id})

            driver_total = q2(sum((D(d["amount"]) for d in driver_facts), D(0)))
            driver_facts_total = facts.add(
                kind="driver_total",
                label=f"Attributed drivers total, {meta.name}, {entity['name']}",
                value=driver_total, unit="currency", period=period, basis="driver",
                entity=code, account=account_code,
                derivation={
                    "type": "driver_sum", "entity": code, "account": account_code,
                    "period": period, "driver": "__all__",
                    "currency": entity["currency"], "rate": str(ledger.rate(code, period)),
                },
                source_system=dataset["meta"]["source_systems"]["plan"],
                source_ref=f"sum of attributed drivers {code}/{account_code}/{period}",
            )

            prior_drivers = _decomposition(ledger, code, account_code, "prior") if material else []

            # Split the attribution into what management controls and what it
            # does not. Both halves are facts, so the split in the narrative is
            # re-derivable.
            discret = q2(sum(
                (D(d["amount"]) for d in driver_facts if d["driver"] not in config.NON_DISCRETIONARY), D(0)
            ))
            nondiscret = q2(sum(
                (D(d["amount"]) for d in driver_facts if d["driver"] in config.NON_DISCRETIONARY), D(0)
            ))
            category_facts = {}
            for key, value in (("discretionary", discret), ("nondiscretionary", nondiscret)):
                category_facts[key] = facts.add(
                    kind="driver_category",
                    label=f"{key.title()} drivers, {meta.name}, {entity['name']}",
                    value=value, unit="currency", period=period, basis="driver",
                    entity=code, account=account_code,
                    derivation={
                        "type": "driver_category_sum", "entity": code, "account": account_code,
                        "period": period, "controllable": key == "discretionary",
                        "currency": entity["currency"], "rate": str(ledger.rate(code, period)),
                    },
                    source_system=dataset["meta"]["source_systems"]["plan"],
                    source_ref=f"driver taxonomy split {code}/{account_code}/{period}",
                ).fact_id

            variances.append({
                "variance_id": vid,
                "entity": code,
                "entity_name": entity["name"],
                "account": account_code,
                "account_name": meta.name,
                "account_short": meta.short,
                "statement": meta.statement,
                "cls": meta.cls,
                "currency": US,
                "period": period,
                "actual_usd": str(actual_usd),
                "budget_usd": str(budget_usd),
                "prior_usd": str(prior_usd),
                "forecast_usd": str(forecast_usd),
                "variance_usd": str(variance_usd),
                "variance_pct": str(variance_pct),
                "variance_prior_usd": str(prior_var),
                "variance_prior_pct": str(prior_pct),
                "variance_forecast_usd": str(forecast_var),
                "material": material,
                "materiality_reason": reason,
                "threshold_usd": str(policy["amount_usd"]),
                "threshold_pct": str(policy["min_pct"]),
                "favorability": _favorability(variance_usd, account_code),
                "direction": _direction(variance_usd),
                "fact_ids": fact_ids,
                "drivers": driver_facts,
                "driver_total_usd": str(driver_total),
                "driver_closure": {
                    "sum_usd": str(driver_total),
                    "variance_usd": str(variance_usd),
                    "difference_usd": str(q2(driver_total - variance_usd)),
                    "ok": q2(driver_total - variance_usd) == 0,
                },
                "prior_decomposition": prior_drivers,
                "category_fact_ids": category_facts,
                "commentary_required": material,
            })

    # ---- 4. Control results ---------------------------------------------
    integrity: list[dict] = []

    def control(check_id: str, kind: str, target: str, expected, actual, severity_if_break: str, note: str) -> None:
        expected_d, actual_d = D(expected), D(actual)
        difference = q2(actual_d - expected_d)
        status = "pass" if difference == 0 else "break"
        if status == "break" and abs(difference) >= config.INTEGRITY_BLOCK_THRESHOLD_USD:
            severity = "block"
        elif status == "break":
            severity = severity_if_break
        else:
            severity = "info"
        integrity.append({
            "check_id": check_id,
            "kind": kind,
            "target": target,
            "expected": str(q2(expected_d)),
            "actual": str(q2(actual_d)),
            "difference": str(difference),
            "status": status,
            "severity": severity,
            "note": note,
        })

    # 4a. Roll-forward: opening + postings = closing, for every balance-sheet account.
    for entity in dataset["entities"]:
        code = entity["code"]
        for account_code in [a.code for a in config.ACCOUNT_LIST if a.statement == "BS"]:
            for p in ledger.periods:
                closing = ledger.closing_local(code, account_code, p)
                if closing is None:
                    continue
                prev_index = ledger.periods.index(p) - 1
                opening_period = "2026-06" if prev_index < 0 else ledger.periods[prev_index]
                opening = ledger.closing_local(code, account_code, opening_period)
                if opening is None:
                    continue
                movement = ACCOUNTS[account_code].presentation_sign * ledger.ledger_sum(code, account_code, p)
                control(
                    check_id=f"RF-{code}-{account_code}-{p}",
                    kind="roll_forward",
                    target=f"{code} {account_code} {p}",
                    expected=closing,
                    actual=D(opening) + movement,
                    severity_if_break="warn",
                    note="Opening balance plus period postings equals the reported closing balance.",
                )

    # 4b. Subledger tie-out for cash.
    for entity in dataset["entities"]:
        code = entity["code"]
        for p in ledger.periods:
            row = ledger.subledger_row(code, "1000", p)
            if not row:
                continue
            rate = ledger.rate(code, p)
            control(
                check_id=f"SL-{code}-1000-{p}",
                kind="subledger_tie",
                target=f"{code} cash subledger {p}",
                expected=D(row["subledger_closing_local"]) * rate,
                actual=D(row["gl_closing_local"]) * rate,
                severity_if_break="warn",
                note="General ledger cash balance agrees to the subledger statement of account.",
            )

    # 4c. Intercompany elimination.
    ic_results: list[dict] = []
    for row in dataset["intercompany"]:
        if row["period"] != period:
            continue
        rate_recv = ledger.fx[period][row["receivable_currency"]]
        rate_pay = ledger.fx[period][row["payable_currency"]]
        receivable_usd = q2(D(row["receivable_local"]) * rate_recv)
        payable_usd = q2(D(row["payable_local"]) * rate_pay)
        difference = q2(receivable_usd - payable_usd)
        status = "pass" if difference == 0 else "break"
        ic_results.append({
            "pair_id": row["pair_id"],
            "lender": row["lender"],
            "borrower": row["borrower"],
            "lender_name": ENTITY_BY_CODE[row["lender"]].name,
            "borrower_name": ENTITY_BY_CODE[row["borrower"]].name,
            "purpose": row["purpose"],
            "receivable_local": row["receivable_local"],
            "receivable_currency": row["receivable_currency"],
            "payable_local": row["payable_local"],
            "payable_currency": row["payable_currency"],
            "receivable_usd": str(receivable_usd),
            "payable_usd": str(payable_usd),
            "difference_usd": str(difference),
            "difference_pct": str(pct(difference, receivable_usd) if receivable_usd else 0),
            "status": status,
            "note": row["note"],
        })
        control(
            check_id=f"IC-{row['pair_id']}-{period}",
            kind="intercompany",
            target=f"{row['lender']} / {row['borrower']} {row['pair_id']}",
            expected=payable_usd,
            actual=receivable_usd,
            severity_if_break="warn",
            note=row["note"],
        )

    # 4d. FX completeness.
    for entity in dataset["entities"]:
        code = entity["code"]
        cur = entity["currency"]
        missing = [p for p in ledger.periods if cur not in ledger.fx.get(p, {})]
        control(
            check_id=f"FX-{code}",
            kind="fx_completeness",
            target=f"{code} {cur} closing rates",
            expected=len(ledger.periods),
            actual=len(ledger.periods) - len(missing),
            severity_if_break="block",
            note="A month-end closing rate exists for every period in scope.",
        )

    # 4e. Plan completeness for every account with ledger activity.
    for entity in dataset["entities"]:
        code = entity["code"]
        missing = []
        for account_code in [a.code for a in config.ACCOUNT_LIST]:
            if not ledger.lines(code, account_code, period):
                continue
            row = ledger._plan.get((code, account_code, period))
            if row is None:
                missing.append(account_code)
        control(
            check_id=f"PLAN-{code}",
            kind="plan_completeness",
            target=f"{code} plan coverage {period}",
            expected=1,
            actual=0 if missing else 1,
            severity_if_break="warn",
            note="Every account with ledger activity has a budget row." if not missing else f"Missing plan rows: {', '.join(missing)}",
        )

    # 4f. Journal period completeness (no posting gap for active accounts).
    for entity in dataset["entities"]:
        code = entity["code"]
        gaps = 0
        checked = 0
        for account_code in [a.code for a in config.ACCOUNT_LIST]:
            if not ledger.lines(code, account_code, period):
                continue
            checked += 1
            if any(not ledger.lines(code, account_code, p) for p in ledger.periods):
                gaps += 1
        control(
            check_id=f"PC-{code}",
            kind="period_completeness",
            target=f"{code} posting continuity",
            expected=checked,
            actual=checked - gaps,
            severity_if_break="warn",
            note="Active accounts have postings in every period of the quarter.",
        )

    # 4g. Trial balance proof: debit balances must equal credit balances.
    # A trial balance that does not prove is not a disclosed exception, it is
    # a broken ledger. Both the entity and group controls block on any break.
    for entity in dataset["entities"]:
        code = entity["code"]
        rows = [r for r in trial_balance if r["entity"] == code]
        total_debit = sum((D(r["debit_usd"]) for r in rows), D(0))
        total_credit = sum((D(r["credit_usd"]) for r in rows), D(0))
        control(
            check_id=f"TB-{code}",
            kind="trial_balance",
            target=f"{code} trial balance {period}",
            expected=total_debit,
            actual=total_credit,
            severity_if_break="block",
            note="Total debit balances equal total credit balances, including retained earnings brought forward.",
        )

    total_debit = sum((D(r["debit_usd"]) for r in trial_balance), D(0))
    total_credit = sum((D(r["credit_usd"]) for r in trial_balance), D(0))
    control(
        check_id="TB-GRP",
        kind="trial_balance",
        target=f"group trial balance {period}",
        expected=total_debit,
        actual=total_credit,
        severity_if_break="block",
        note="Consolidated debit balances equal consolidated credit balances at closing rates.",
    )

    breaks = [c for c in integrity if c["status"] == "break"]
    material_variances = [v for v in variances if v["material"]]

    stats = {
        "entities": len(dataset["entities"]),
        "accounts_in_scope": len({v["account"] for v in variances}),
        "journal_lines": len(dataset["journal"]),
        "trial_balance_rows": len(trial_balance),
        "facts": len(facts),
        "variances": len(variances),
        "material_variances": len(material_variances),
        "pl_material": len([v for v in material_variances if v["statement"] == "PL"]),
        "bs_material": len([v for v in material_variances if v["statement"] == "BS"]),
        "controls_run": len(integrity),
        "controls_breaks": len(breaks),
        "ic_pairs": len(ic_results),
        "ic_breaks": len([i for i in ic_results if i["status"] == "break"]),
        "total_variance_usd": str(q2(sum((D(v["variance_usd"]) for v in variances if v["statement"] == "PL"), D(0)))),
        "net_unfavourable_usd": str(q2(sum(
            (D(v["variance_usd"]) for v in material_variances if v["favorability"] == "unfavorable"), D(0)
        ))),
        "net_favourable_usd": str(q2(sum(
            (D(v["variance_usd"]) for v in material_variances if v["favorability"] == "favorable"), D(0)
        ))),
        "fx_rates": {p: {c: str(r) for c, r in rates.items()} for p, rates in ledger.fx.items()},
    }

    consolidated = _consolidate(ledger, dataset)
    aggregates = _add_aggregate_facts(facts, ledger, dataset)
    stats["facts"] = len(facts)

    return {
        "stage": "reconcile",
        "engine_version": config.ENGINE_VERSION,
        "period": period,
        "prior_period": prior_period,
        "group_currency": US,
        "policy": {
            "PL": {"amount_usd": str(MATERIALITY["PL"]["amount_usd"]), "min_pct": str(MATERIALITY["PL"]["min_pct"])},
            "BS": {"amount_usd": str(MATERIALITY["BS"]["amount_usd"]), "min_pct": str(MATERIALITY["BS"]["min_pct"])},
            "integrity_block_threshold_usd": str(config.INTEGRITY_BLOCK_THRESHOLD_USD),
        },
        "trial_balance": trial_balance,
        "consolidated": consolidated,
        "aggregates": aggregates,
        "variances": variances,
        "intercompany": ic_results,
        "integrity": integrity,
        "stats": stats,
        "exceptions": dataset["exceptions"],
        "facts": facts.to_list(),
    }


def _consolidate(ledger: Ledger, dataset: dict) -> list[dict]:
    """Group totals by account, summing translated entity balances."""
    rows: list[dict] = []
    period = ledger.current
    for account in config.ACCOUNT_LIST:
        actual = q2(sum((ledger.presented_actual_usd(e["code"], account.code, period) for e in dataset["entities"]), D(0)))
        budget = q2(sum((ledger.plan_usd(e["code"], account.code, period, "budget") for e in dataset["entities"]), D(0)))
        prior = q2(sum((ledger.presented_actual_usd(e["code"], account.code, ledger.prior) for e in dataset["entities"]), D(0)))
        forecast = q2(sum((ledger.plan_usd(e["code"], account.code, period, "forecast") for e in dataset["entities"]), D(0)))
        if actual == 0 and budget == 0:
            continue
        variance = q2(actual - budget)
        rows.append({
            "account": account.code,
            "account_name": account.name,
            "account_short": account.short,
            "statement": account.statement,
            "actual_usd": str(actual),
            "budget_usd": str(budget),
            "prior_usd": str(prior),
            "forecast_usd": str(forecast),
            "variance_usd": str(variance),
            "variance_pct": str(pct(variance, abs(budget)) if budget else 0),
        })
    return rows


# ---------------------------------------------------------------------------
# Group aggregates — the numbers the CFO summary actually talks about
# ---------------------------------------------------------------------------
AGGREGATES: list[dict] = [
    {"id": "revenue_total", "label": "Revenue", "accounts": [("4000", 1), ("4100", 1)]},
    {"id": "cost_of_revenue", "label": "Cost of revenue", "accounts": [("5000", 1)]},
    {"id": "gross_profit", "label": "Gross profit", "accounts": [("4000", 1), ("4100", 1), ("5000", -1)]},
    {"id": "opex_total", "label": "Operating expenses", "accounts": [
        ("6000", 1), ("6100", 1), ("6200", 1), ("6300", 1), ("6400", 1), ("6500", 1)]},
    {"id": "ebitda", "label": "EBITDA", "accounts": [
        ("4000", 1), ("4100", 1), ("5000", -1), ("6000", -1), ("6100", -1),
        ("6200", -1), ("6300", -1), ("6400", -1), ("6500", -1)]},
    {"id": "operating_income", "label": "Operating income", "accounts": [
        ("4000", 1), ("4100", 1), ("5000", -1), ("6000", -1), ("6100", -1),
        ("6200", -1), ("6300", -1), ("6400", -1), ("6500", -1), ("7000", -1)]},
    {"id": "net_income", "label": "Net income", "accounts": [
        ("4000", 1), ("4100", 1), ("5000", -1), ("6000", -1), ("6100", -1),
        ("6200", -1), ("6300", -1), ("6400", -1), ("6500", -1), ("7000", -1),
        ("8000", -1), ("9000", -1)]},
]


def _aggregate_value(ledger: Ledger, dataset: dict, accounts: list, period: str, basis: str) -> Decimal:
    if basis == "actual":
        return q2(sum(
            (sign * ledger.presented_actual_usd(e["code"], code, period) for code, sign in accounts for e in dataset["entities"]),
            D(0),
        ))
    if basis == "prior":
        return q2(sum(
            (sign * ledger.presented_actual_usd(e["code"], code, ledger.prior) for code, sign in accounts for e in dataset["entities"]),
            D(0),
        ))
    return q2(sum(
        (sign * ledger.plan_usd(e["code"], code, period, basis) for code, sign in accounts for e in dataset["entities"]),
        D(0),
    ))


def _add_aggregate_facts(facts: FactStore, ledger: Ledger, dataset: dict) -> list[dict]:
    period = ledger.current
    out: list[dict] = []
    for spec in AGGREGATES:
        row: dict = {
            "aggregate_id": spec["id"],
            "label": spec["label"],
            "fact_ids": {},
            "value_usd": {},
        }
        for basis in ("actual", "budget", "prior", "forecast"):
            value = _aggregate_value(ledger, dataset, spec["accounts"], period, basis)
            fact = facts.add(
                kind="aggregate",
                label=f"{spec['label']} ({basis}), group, {config.PERIOD_LABELS[period]}",
                value=value,
                unit="currency",
                period=period if basis != "prior" else ledger.prior,
                basis=basis,
                entity="GRP",
                account=None,
                aggregate=spec["id"],
                derivation={
                    "type": "aggregate",
                    "accounts": [[code, sign] for code, sign in spec["accounts"]],
                    "period": period,
                    "basis": basis,
                },
                source_system="close-engine (consolidated)",
                source_ref=f"sum of entity balances for {spec['id']}",
            )
            row["fact_ids"][basis] = fact.fact_id
            row["value_usd"][basis] = str(value)

        var = q2(D(row["value_usd"]["actual"]) - D(row["value_usd"]["budget"]))
        var_fact = facts.add(
            kind="aggregate_variance",
            label=f"{spec['label']} variance vs budget, group",
            value=var, unit="currency", period=period, basis="variance",
            entity="GRP", account=None, aggregate=spec["id"], comparator="budget",
            derivation={"type": "difference", "left": row["fact_ids"]["actual"], "right": row["fact_ids"]["budget"]},
            source_system="close-engine (derived)",
            source_ref=f"{spec['id']} actual less budget",
        )
        prior_var = q2(D(row["value_usd"]["actual"]) - D(row["value_usd"]["prior"]))
        row["fact_ids"]["variance"] = var_fact.fact_id
        row["value_usd"]["variance"] = str(var)
        row["variance_pct"] = str(pct(var, abs(D(row["value_usd"]["budget"]))) if D(row["value_usd"]["budget"]) else 0)
        pct_fact = facts.add(
            kind="aggregate_variance_pct",
            label=f"{spec['label']} variance vs budget %, group",
            value=D(row["variance_pct"]), unit="pct", period=period, basis="variance",
            entity="GRP", account=None, aggregate=spec["id"], comparator="budget",
            derivation={"type": "ratio", "numerator": var_fact.fact_id, "denominator": row["fact_ids"]["budget"]},
            source_system="close-engine (derived)",
            source_ref=f"{spec['id']} variance over budget",
        )
        row["fact_ids"]["variance_pct"] = pct_fact.fact_id
        row["prior_variance_usd"] = str(prior_var)
        out.append(row)
    return out
