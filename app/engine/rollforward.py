"""Multi-period roll-forward.

The monthly close is a single period; a controller also reads the quarter as a
period × account pivot. This module reconciles each configured period with the
normal engine and pivots the variance queue, so the roll-forward is arithmetic
from the same source of truth rather than a second reporting path.

Group (`GRP`) rows are the sum of entity balances, with materiality recomputed
at group level: a movement can sit below every entity's floor and still clear
the group floor.
"""
from __future__ import annotations

from app import config
from app.config import ACCOUNTS
from app.engine.reconcile import _is_material, reconcile
from app.money import D, q2

_CACHE: dict[str, object] = {"key": None, "value": None}


def dataset_for_period(dataset: dict, period: str) -> dict:
    """Re-scope the dataset to `period`, including the correct prior month.

    The extract starts at the first configured period, so the first month has
    no prior month to compare against. It falls back to itself (a zero prior
    variance) rather than borrowing a later month's balances.
    """
    periods = dataset["meta"]["periods"]
    index = periods.index(period)
    prior = periods[index - 1] if index > 0 else periods[0]
    return {
        **dataset,
        "meta": {
            **dataset["meta"],
            "current_period": period,
            "prior_period": prior,
        },
    }


def _cell(variance: dict) -> dict:
    return {
        "actual_usd": variance["actual_usd"],
        "budget_usd": variance["budget_usd"],
        "prior_usd": variance["prior_usd"],
        "forecast_usd": variance["forecast_usd"],
        "variance_usd": variance["variance_usd"],
        "variance_pct": variance["variance_pct"],
        "variance_prior_usd": variance["variance_prior_usd"],
        "material": bool(variance["material"]),
        "materiality_reason": variance["materiality_reason"],
        "favorability": variance["favorability"],
    }


def _group_cell(row: dict) -> dict:
    variance = D(row["variance_usd"])
    variance_pct = D(row["variance_pct"])
    material, reason = _is_material(variance, variance_pct, row["account"])
    actual = D(row["actual_usd"])
    prior = D(row["prior_usd"])
    account = ACCOUNTS[row["account"]]
    favorability = "neutral"
    if variance != 0 and account.favorability != "neutral":
        higher_better = account.favorability == "higher_better"
        favorability = "favorable" if (variance > 0) == higher_better else "unfavorable"
    return {
        "actual_usd": row["actual_usd"],
        "budget_usd": row["budget_usd"],
        "prior_usd": row["prior_usd"],
        "forecast_usd": row["forecast_usd"],
        "variance_usd": row["variance_usd"],
        "variance_pct": row["variance_pct"],
        "variance_prior_usd": str(q2(actual - prior)),
        "material": bool(material),
        "materiality_reason": reason,
        "favorability": favorability,
    }


def build_rollforward(dataset: dict) -> dict:
    """Reconcile every configured period and pivot entity × account × period."""
    periods = list(dataset["meta"]["periods"])
    cache_key = (
        dataset["meta"]["current_period"],
        len(dataset["journal"]),
        len(dataset["plan"]),
        len(dataset["drivers"]),
    )
    if _CACHE["key"] == cache_key and _CACHE["value"] is not None:
        return _CACHE["value"]  # type: ignore[return-value]

    recons: dict[str, dict] = {}
    for period in periods:
        recons[period] = reconcile(dataset_for_period(dataset, period))

    rows: dict[tuple[str, str], dict] = {}
    for period in periods:
        recon = recons[period]
        for variance in recon["variances"]:
            key = (variance["entity"], variance["account"])
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "row_id": f"{variance['entity']}|{variance['account']}",
                    "entity": variance["entity"],
                    "entity_name": variance["entity_name"],
                    "group": False,
                    "account": variance["account"],
                    "account_name": variance["account_name"],
                    "account_short": variance["account_short"],
                    "statement": variance["statement"],
                    "cls": variance["cls"],
                    "periods": {},
                }
            row["periods"][period] = _cell(variance)

        for consolidated in recon["consolidated"]:
            key = ("GRP", consolidated["account"])
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "row_id": f"GRP|{consolidated['account']}",
                    "entity": "GRP",
                    "entity_name": config.GROUP_NAME,
                    "group": True,
                    "account": consolidated["account"],
                    "account_name": consolidated["account_name"],
                    "account_short": consolidated["account_short"],
                    "statement": consolidated["statement"],
                    "cls": ACCOUNTS[consolidated["account"]].cls,
                    "periods": {},
                }
            row["periods"][period] = _group_cell(consolidated)

    ordered = sorted(
        rows.values(),
        key=lambda r: (0 if r["group"] else 1, r["account"], r["entity"]),
    )
    for row in ordered:
        row["material_periods"] = [
            period for period in periods
            if row["periods"].get(period, {}).get("material")
        ]
        row["threshold"] = {
            "amount_usd": str(config.MATERIALITY[row["statement"]]["amount_usd"]),
            "min_pct": str(config.MATERIALITY[row["statement"]]["min_pct"]),
        }

    aggregates = {
        period: [
            {
                "aggregate_id": aggregate["aggregate_id"],
                "label": aggregate["label"],
                "value_usd": aggregate["value_usd"],
                "variance_pct": aggregate["variance_pct"],
                "prior_variance_usd": aggregate["prior_variance_usd"],
            }
            for aggregate in recons[period]["aggregates"]
        ]
        for period in periods
    }

    result = {
        "stage": "rollforward",
        "engine_version": config.ENGINE_VERSION,
        "periods": periods,
        "period_labels": {period: config.PERIOD_LABELS.get(period, period) for period in periods},
        "current_period": dataset["meta"]["current_period"],
        "group_currency": config.GROUP_CURRENCY,
        "entities": [{"code": e["code"], "name": e["name"]} for e in dataset["entities"]],
        "rows": ordered,
        "aggregates": aggregates,
        "stats": {
            "periods": len(periods),
            "rows": len(ordered),
            "entity_rows": len([r for r in ordered if not r["group"]]),
            "group_rows": len([r for r in ordered if r["group"]]),
            "material_cells": sum(
                1 for r in ordered for period in periods
                if r["periods"].get(period, {}).get("material")
            ),
        },
    }
    _CACHE["key"] = cache_key
    _CACHE["value"] = result
    return result
