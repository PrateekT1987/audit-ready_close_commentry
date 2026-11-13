"""Static configuration for the financial close commentary engine.

The domain model here is deliberately small but complete: a multi-entity group
reporting in USD, a chart of accounts with ledger sign conventions, and the
materiality policy that decides which variances need human-grade commentary.

Every number in the product ultimately traces back to one of three sources of
truth defined in this module:

  * the subledger journal lines (the general ledger)
  * the plan of record (budget / forecast, from the CPM system)
  * the FX rate table published by group treasury
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal

GROUP_CURRENCY = "USD"
GROUP_NAME = "Acme Global Holdings"

PERIODS = ["2026-07", "2026-08", "2026-09", "2026-10"]
CURRENT_PERIOD = "2026-10"
PRIOR_PERIOD = "2026-09"

PERIOD_LABELS = {
    "2026-07": "July 2026",
    "2026-08": "August 2026",
    "2026-09": "September 2026",
    "2026-10": "October 2026",
}

# Group treasury month-end closing rates, local currency -> USD.
FX_RATES: dict[str, dict[str, Decimal]] = {
    "2026-07": {"USD": Decimal("1"), "EUR": Decimal("1.0910"), "GBP": Decimal("1.2830"), "SGD": Decimal("0.7480")},
    "2026-08": {"USD": Decimal("1"), "EUR": Decimal("1.0875"), "GBP": Decimal("1.2785"), "SGD": Decimal("0.7455")},
    "2026-09": {"USD": Decimal("1"), "EUR": Decimal("1.0740"), "GBP": Decimal("1.2660"), "SGD": Decimal("0.7405")},
    "2026-10": {"USD": Decimal("1"), "EUR": Decimal("1.0620"), "GBP": Decimal("1.2540"), "SGD": Decimal("0.7360")},
}

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "SGD": "S$"}


@dataclass(frozen=True)
class Entity:
    code: str
    name: str
    currency: str
    region: str
    country: str


ENTITIES: list[Entity] = [
    Entity("US01", "Acme US Inc.", "USD", "Americas", "United States"),
    Entity("DE02", "Acme GmbH", "EUR", "EMEA", "Germany"),
    Entity("SG03", "Acme APAC Pte. Ltd.", "SGD", "APAC", "Singapore"),
    Entity("UK04", "Acme UK Ltd.", "GBP", "EMEA", "United Kingdom"),
]

ENTITY_BY_CODE = {e.code: e for e in ENTITIES}


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    short: str
    cls: str          # revenue | cogs | opex | other | asset | liability
    statement: str    # PL | BS
    # Ledger convention below is debit-positive. Assets and expenses are
    # debit-normal (+1); revenue and liabilities are credit-normal (-1).
    presentation_sign: int
    # Which direction of movement is good news for management commentary.
    favorability: str  # higher_better | higher_worse | neutral
    phrase: str = ""   # how the account is named in commentary

    def to_dict(self) -> dict:
        return asdict(self)


_ACCOUNTS: list[Account] = [
    Account("1000", "Cash and Cash Equivalents", "Cash", "asset", "BS", 1, "neutral", "Cash and cash equivalents"),
    Account("1100", "Trade Accounts Receivable", "AR", "asset", "BS", 1, "neutral", "Trade accounts receivable"),
    Account("1200", "Inventory", "Inventory", "asset", "BS", 1, "neutral", "Inventory"),
    Account("1300", "Intercompany Receivable", "IC Receivable", "asset", "BS", 1, "neutral", "Intercompany receivable"),
    Account("2000", "Trade Accounts Payable", "AP", "liability", "BS", -1, "neutral", "Trade accounts payable"),
    Account("2100", "Accrued Liabilities", "Accruals", "liability", "BS", -1, "neutral", "Accrued liabilities"),
    Account("2200", "Intercompany Payable", "IC Payable", "liability", "BS", -1, "neutral", "Intercompany payable"),
    Account("3000", "Deferred Revenue", "Deferred Rev.", "liability", "BS", -1, "neutral", "Deferred revenue"),
    Account("4000", "Revenue", "Revenue", "revenue", "PL", -1, "higher_better", "Revenue"),
    Account("4100", "Other Revenue", "Other Revenue", "revenue", "PL", -1, "higher_better", "Other revenue"),
    Account("5000", "Cost of Revenue", "Cost of Revenue", "cogs", "PL", 1, "higher_worse", "Cost of revenue"),
    Account("6000", "Salaries and Wages", "Salaries", "opex", "PL", 1, "higher_worse", "Salaries and wages expense"),
    Account("6100", "Sales and Marketing", "S&M", "opex", "PL", 1, "higher_worse", "Sales and marketing expense"),
    Account("6200", "Research and Development", "R&D", "opex", "PL", 1, "higher_worse", "Research and development expense"),
    Account("6300", "General and Administrative", "G&A", "opex", "PL", 1, "higher_worse", "General and administrative expense"),
    Account("6400", "Professional Fees", "Professional Fees", "opex", "PL", 1, "higher_worse", "Professional fees"),
    Account("6500", "Travel and Entertainment", "T&E", "opex", "PL", 1, "higher_worse", "Travel and entertainment expense"),
    Account("7000", "Depreciation and Amortization", "D&A", "other", "PL", 1, "higher_worse", "Depreciation and amortization"),
    Account("8000", "Interest Expense", "Interest", "other", "PL", 1, "higher_worse", "Interest expense"),
    Account("9000", "Provision for Income Tax", "Tax", "other", "PL", 1, "higher_worse", "Income tax expense"),
]

ACCOUNTS: dict[str, Account] = {a.code: a for a in _ACCOUNTS}
ACCOUNT_LIST = _ACCOUNTS


# ---------------------------------------------------------------------------
# Materiality policy (the "what needs a story" rule)
# ---------------------------------------------------------------------------
# A variance is actionable when it clears BOTH the absolute amount floor and
# the relative floor for its statement. Both floors are policy, not judgement
# calls made by the agent, so coverage can be measured mechanically.
MATERIALITY = {
    "PL": {"amount_usd": Decimal("25000"), "min_pct": Decimal("0.02")},
    "BS": {"amount_usd": Decimal("50000"), "min_pct": Decimal("0.01")},
}

# Any fact-level disagreement with the source system at or above this amount
# is escalated to a blocking finding rather than a disclosed exception.
INTEGRITY_BLOCK_THRESHOLD_USD = Decimal("50000")

# ---------------------------------------------------------------------------
# Driver taxonomy — the vocabulary used to attribute a variance to a cause.
# ---------------------------------------------------------------------------
DRIVERS: dict[str, str] = {
    "D_VOLUME": "Volume",
    "D_PRICE": "Price and rate",
    "D_MIX": "Product and customer mix",
    "D_FX": "FX translation",
    "D_HEADCOUNT": "Headcount",
    "D_COMP": "Compensation and merit",
    "D_MARKETING": "Marketing programmes",
    "D_SUPPLIER": "Supplier and input costs",
    "D_ONEOFF": "One-off items",
    "D_TIMING": "Timing and cut-off",
    "D_POLICY": "Policy and control actions",
    "D_OTHER": "Other, net",
}

# Driver families that are genuinely outside management control (used to
# classify commentary for the CFO summary).
NON_DISCRETIONARY = {"D_FX", "D_ONEOFF"}

STAGE_ORDER = ["reconcile", "narrate", "verify", "signoff"]
STAGE_LABELS = {
    "reconcile": "Reconcile",
    "narrate": "Narrate",
    "verify": "Verify",
    "signoff": "Sign-off",
}
