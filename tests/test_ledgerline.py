"""Ledgerline test suite — plain Python, no pytest required.

    .venv/bin/python -m tests.test_ledgerline

Covers the money formatter/parser, the reconcile invariants, the fact-bound
narration plan, both verifier outcomes (clean and adversarial), the sign-off
chain, tamper detection and the HTTP surface end to end.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import traceback
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

# The HTTP tests must not touch the workpaper database used by the dev server.
_TMP = Path(tempfile.mkdtemp(prefix="ledgerline-tests-"))
os.environ["FINCOM_DB"] = str(_TMP / "close.db")
# The suite runs in the local/test security profile. Production/staging are
# fail-closed and are covered by the settings-validation tests below.
os.environ["APP_ENV"] = "test"
os.environ["AUTH_MODE"] = "dev"
os.environ["SESSION_SECRET"] = "test-session-secret-test-session-secret-123456"
os.environ["ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
os.environ["DEV_AUTH_HEADERS"] = "1"

from app import config, store  # noqa: E402
from app.engine import audit  # noqa: E402
from app.engine import verify as verify_engine  # noqa: E402
from app.engine.narrate import narrate  # noqa: E402
from app.engine.reconcile import reconcile  # noqa: E402
from app.engine.verify import verify  # noqa: E402
from app.money import (  # noqa: E402
    ParseError,
    expected_display_number,
    find_figures_in_text,
    fmt_money,
    fmt_pct,
    parse_display,
)

RAW: dict = {}
RECON: dict = {}
STRICT: dict = {}
VERIFIED: dict = {}
DRIFT: dict = {}
DRIFT_VERIFIED: dict = {}


def setup() -> None:
    global RAW, RECON, STRICT, VERIFIED, DRIFT, DRIFT_VERIFIED
    RAW = store.load_dataset()
    RECON = reconcile(RAW)
    STRICT = narrate(RECON, mode="strict")
    VERIFIED = verify(RAW, RECON, STRICT)
    DRIFT = narrate(RECON, mode="drift")
    DRIFT_VERIFIED = verify(RAW, RECON, DRIFT)


# --------------------------------------------------------------- money
def test_money_round_trip() -> None:
    values = [
        "-1234567890.12", "-1234567.89", "-1234.56", "-12.34",
        "0", "0.004", "7.5", "999.99", "1234.56", "14922240", "1234567890",
    ]
    for value in values:
        rendered = fmt_money(value)
        parsed = parse_display(rendered["text"])
        expected = expected_display_number(value, "currency", rendered["scale"], rendered["decimals"])
        assert parsed["number"] == expected, f"{value} rendered {rendered['text']!r}"
        assert parsed["unit"] == "currency"
        assert parsed["decimals"] == rendered["decimals"]


def test_pct_round_trip() -> None:
    rendered = fmt_pct("0.1764")
    assert rendered["text"] == "17.6%"
    parsed = parse_display(rendered["text"])
    assert parsed["number"] == Decimal("17.6")
    assert parsed["unit"] == "pct"


def test_parser_rejects_prose() -> None:
    for bad in ("about $40K", "$1.2M to $1.4M", "", "n/a"):
        try:
            parse_display(bad)
        except ParseError:
            continue
        raise AssertionError(f"parse_display accepted {bad!r}")


def test_quantity_scanner_catches_spelled_and_vague_quantities() -> None:
    # Anything that reads as a quantity is a figure, however it is written.
    cases = {
        "forty thousand dollars of accrued spend": "forty thousand dollars",
        "roughly 40 of accrued spend": "roughly 40",
        "a fifth of revenue": "a fifth of",
        "two-thirds of the movement": "two-thirds of",
        "fifteen percent of the base": "fifteen percent",
        "two million in accruals": "two million",
        "hundreds of invoices": "hundreds of",
        "40 days": "40",
    }
    for text, expected in cases.items():
        found = find_figures_in_text(text)
        assert expected in found, f"{text!r} -> {found!r}"

    # Prose that only looks numeric stays prose: years, quarter labels,
    # hyphenated compounds and unitless spelled counts.
    for text in (
        "October 2026",
        "Q4 brand campaign",
        "one-off items",
        "two regional field events",
        "net twenty-two hires in engineering",
        "the top twenty accounts",
        "the remaining quarter",
    ):
        assert find_figures_in_text(text) == [], text


def test_spelled_out_quantity_blocks_the_section() -> None:
    sections = copy.deepcopy(STRICT["sections"])
    sections[0]["paragraphs"][0]["tokens"].append({
        "t": "text",
        "v": " A further forty thousand dollars of accrued spend is expected to unwind.",
    })
    findings = verify_engine.Findings()
    facts = {f["fact_id"]: f for f in RECON["facts"]}
    verify_engine._check_sections(sections, facts, RECON, findings, check_coverage=False)
    blocks = [f for f in findings.items if f["severity"] == "block"]
    assert [f["category"] for f in blocks] == ["UNSOURCED_FIGURE"]
    assert blocks[0]["claim"] == "forty thousand dollars"
    assert blocks[0]["section_id"] == STRICT["sections"][0]["section_id"]


# ------------------------------------------------------------- reconcile
def test_reconcile_invariants() -> None:
    stats = RECON["stats"]
    assert stats["entities"] == 4
    assert stats["journal_lines"] == 1072
    assert stats["accounts_in_scope"] == 20
    assert stats["variances"] == 76
    assert stats["material_variances"] == 12
    assert stats["pl_material"] == 11 and stats["bs_material"] == 1
    assert stats["controls_run"] == 151
    assert stats["controls_breaks"] == 3
    assert stats["facts"] == len(RECON["facts"]) == 862
    assert stats["trial_balance_rows"] == 80
    assert len({f["fact_id"] for f in RECON["facts"]}) == 862
    assert all(f["derivation"] for f in RECON["facts"])
    # Every breaking control is a disclosed warning, never silently dropped.
    breaks = [c for c in RECON["integrity"] if c["status"] == "break"]
    assert len(breaks) == 3
    assert all(b["difference"] != "0.00" for b in breaks)

    # The ledger itself is double-entry: every entity-period posts equal
    # debits and credits, and each line carries exactly one side.
    debits = defaultdict(Decimal)
    credits = defaultdict(Decimal)
    for line in RAW["journal"]:
        key = (line["entity"], line["period"])
        debits[key] += Decimal(line["debit"])
        credits[key] += Decimal(line["credit"])
    assert debits == credits
    assert all(line["debit"] == "0.00" or line["credit"] == "0.00" for line in RAW["journal"])

    # Every entity and the group prove: debit balances equal credit balances.
    tb_controls = {c["check_id"]: c for c in RECON["integrity"] if c["kind"] == "trial_balance"}
    assert set(tb_controls) == {"TB-US01", "TB-DE02", "TB-SG03", "TB-UK04", "TB-GRP"}
    assert all(c["status"] == "pass" for c in tb_controls.values())
    assert all(c["difference"] == "0.00" for c in tb_controls.values())


def test_reconcile_is_deterministic() -> None:
    again = reconcile(RAW)
    assert again["stats"] == RECON["stats"]
    assert [v["variance_usd"] for v in again["variances"]] == [v["variance_usd"] for v in RECON["variances"]]
    assert [f["value"] for f in again["facts"]] == [f["value"] for f in RECON["facts"]]


def test_trial_balance_breaks_block_the_run() -> None:
    # A one-sided journal edit: the entity proof and the group proof break,
    # a different entity stays clean, and the severity is blocking.
    tampered = copy.deepcopy(RAW)
    line = next(
        l for l in tampered["journal"]
        if l["entity"] == "US01" and l["account"] == "6000"
        and l["period"] == "2026-10" and Decimal(l["amount"]) > 0
    )
    line["amount"] = str(Decimal(line["amount"]) + Decimal("1000.00"))
    line["debit"] = str(Decimal(line["debit"]) + Decimal("1000.00"))
    recon = reconcile(tampered)
    by_id = {c["check_id"]: c for c in recon["integrity"]}
    assert by_id["TB-US01"]["status"] == "break"
    assert by_id["TB-US01"]["severity"] == "block"
    assert by_id["TB-US01"]["difference"] != "0.00"
    assert by_id["TB-GRP"]["status"] == "break"
    assert by_id["TB-GRP"]["severity"] == "block"
    assert by_id["TB-DE02"]["status"] == "pass"
    assert recon["stats"]["controls_breaks"] == 5

    # The block must reach the gate, not merely sit in the reconcile report.
    verification = verify(tampered, recon, narrate(recon, mode="strict"))
    assert verification["gate"]["status"] == "blocked"
    assert verification["gate"]["signable"] is False
    blocked_controls = {
        f["control_id"] for f in verification["findings"]
        if f["severity"] == "block" and f.get("control_id")
    }
    assert {"TB-US01", "TB-GRP"} <= blocked_controls

    # A closing balance that no longer articulates with the period result
    # must break the same proof, with the same blocking severity.
    tampered = copy.deepcopy(RAW)
    row = next(
        r for r in tampered["bs_closing"]
        if r["entity"] == "US01" and r["account"] == "1100" and r["period"] == "2026-10"
    )
    row["closing_local"] = str(Decimal(row["closing_local"]) + Decimal("2500.00"))
    recon = reconcile(tampered)
    by_id = {c["check_id"]: c for c in recon["integrity"]}
    assert by_id["TB-US01"]["status"] == "break"
    assert by_id["TB-US01"]["severity"] == "block"
    assert by_id["TB-GRP"]["status"] == "break"
    assert by_id["TB-GRP"]["severity"] == "block"


# -------------------------------------------------------------- narrate
def test_narration_is_fact_bound() -> None:
    fact_ids = {f["fact_id"] for f in RECON["facts"]}
    assert STRICT["mode"] == "strict"
    assert STRICT["drift"]["applied"] is False
    assert STRICT["stats"]["sections"] == 13
    assert STRICT["stats"]["variance_sections"] == 12
    assert STRICT["stats"]["figures"] == 104
    for section in STRICT["sections"]:
        assert section["paragraphs"]
        for paragraph in section["paragraphs"]:
            assert paragraph["tokens"]
            for token in paragraph["tokens"]:
                assert token["t"] in ("text", "fig")
                if token["t"] == "fig":
                    assert token["fact"] in fact_ids
                    assert token["number"]
                    assert token["unit"] in ("currency", "pct", "count")


def test_narration_uses_the_recon_period() -> None:
    raw = copy.deepcopy(RAW)
    raw["meta"]["current_period"] = "2026-07"
    recon = reconcile(raw)
    commentary = narrate(recon, mode="strict")
    summary = commentary["sections"][0]
    assert recon["period"] == "2026-07"
    assert summary["scope"]["period"] == "2026-07"
    assert "July 2026" in summary["eyebrow"]
    prose = " ".join(
        token["v"]
        for paragraph in summary["paragraphs"]
        for token in paragraph["tokens"]
        if token["t"] == "text"
    )
    assert "July 2026" in prose
    assert "October 2026" not in prose


def test_drift_injects_two_defects() -> None:
    assert DRIFT["mode"] == "drift"
    assert DRIFT["drift"]["applied"] is True
    assert len(DRIFT["drift"]["mutations"]) == 2
    assert DRIFT["drift"]["second_defect_injected"] is True
    assert DRIFT["drift"]["section_id"] == "S-08"


# --------------------------------------------------------------- verify
def test_clean_run_passes_every_layer() -> None:
    stats = VERIFIED["stats"]
    assert VERIFIED["gate"]["status"] == "attestation"
    assert VERIFIED["gate"]["blocking"] == 0
    assert VERIFIED["gate"]["warnings"] == 3
    assert stats["figures_checked"] == 104 and stats["figures_passed"] == 104
    assert stats["facts_checked"] == 862 and stats["facts_passed"] == 862
    assert stats["closure_checks"] == 12 and stats["closure_passed"] == 12
    assert stats["coverage_total"] == 12 and stats["coverage_covered"] == 12
    assert {f["severity"] for f in VERIFIED["findings"]} == {"warn"}
    assert all(c["status"] == "pass" for c in VERIFIED["claims"])


def test_verifier_self_test_detects_every_mutation() -> None:
    self_test = VERIFIED["self_test"]
    assert self_test["total"] == 14 and self_test["detected"] == 14
    assert self_test["passed"] is True
    names = {m["mutation"] for m in self_test["mutations"]}
    assert names == {
        "scale_error", "sign_flip", "rounding_drift", "unsourced_figure",
        "spelled_out_amount", "wrong_scope", "basis_swap",
        "summary_section_scale", "two_figure_sign_and_scale",
        "wrong_comparator_and_scope", "prose_comparator_swap",
        "driver_double_count", "primary_driver_overstated",
        "summary_coverage_overstatement",
    }
    assert all(m["detected"] and m["categories"] for m in self_test["mutations"])


def test_self_test_covers_cross_section_mutations() -> None:
    by_name = {m["mutation"]: m for m in VERIFIED["self_test"]["mutations"]}

    duplicate = by_name["driver_double_count"]
    assert duplicate["detected"] is True
    assert {"DRIVER_DOUBLE_COUNT", "DRIVER_TOTAL_MISMATCH"} <= set(duplicate["categories"])

    primary = by_name["primary_driver_overstated"]
    assert primary["detected"] is True
    assert primary["categories"] == ["PRIMARY_DRIVER_CONFLICT"]

    coverage = by_name["summary_coverage_overstatement"]
    assert coverage["detected"] is True
    assert coverage["categories"] == ["COVERAGE_OVERSTATED"]


def test_self_test_covers_spelled_out_quantities() -> None:
    by_name = {m["mutation"]: m for m in VERIFIED["self_test"]["mutations"]}
    spelled = by_name["spelled_out_amount"]
    assert spelled["detected"] is True
    assert spelled["categories"] == ["UNSOURCED_FIGURE"]
    assert spelled["skipped"] is False


def test_self_test_covers_summary_and_compound_mutations() -> None:
    by_name = {m["mutation"]: m for m in VERIFIED["self_test"]["mutations"]}

    # The summary is the most-read section, so it is attacked directly.
    summary = by_name["summary_section_scale"]
    assert summary["detected"] is True
    assert "VALUE_MISMATCH" in summary["categories"]

    # A sign flip and a scale error in the same paragraph both fail.
    compound = by_name["two_figure_sign_and_scale"]
    assert compound["detected"] is True
    assert "VALUE_MISMATCH" in compound["categories"]
    assert compound["failed_figures"] == 2

    # A wrong comparator and a wrong entity in one section surface both
    # categories, not just whichever figure is checked first.
    both = by_name["wrong_comparator_and_scope"]
    assert both["detected"] is True
    assert {"BASIS_MISMATCH", "SCOPE_MISMATCH"} <= set(both["categories"])
    assert both["failed_figures"] == 2

    # Prose can name the wrong comparator while every figure stays correct.
    prose = by_name["prose_comparator_swap"]
    assert prose["detected"] is True
    assert prose["categories"] == ["COMPARATOR_MISMATCH"]


def _claim_categories(text: str, variance: dict) -> set[str]:
    section = {
        "section_id": "S-99",
        "scope": {"entity": "US01", "account": "1000", "period": "2026-10", "comparator": "budget"},
    }
    findings = verify_engine.Findings()
    verify_engine._check_semantics(section, {"role": "movement"}, text, variance, RECON, findings)
    return findings.categories()


def test_semantic_direction_and_comparator_extraction() -> None:
    claims = verify_engine.extract_direction_claims(
        "Revenue came in at $4.56M, above the revised plan of $4.39M by $167.8K."
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim["subject"] == "Revenue"
    assert claim["verb"] == "came in at"
    assert claim["direction"] == "up"
    assert claim["comparator"] == "forecast"
    assert claim["comparator_phrase"] == "revised plan"

    inline = verify_engine.extract_direction_claims("Cash came in at $0.00, in line with plan.")
    assert inline[0]["direction"] == "inline"
    assert inline[0]["comparator"] == "budget"

    # One sentence can carry two claims with different subjects and directions.
    multi = verify_engine.extract_direction_claims(
        "Operating expenses of $7.20M were above plan by $474.4K, so operating income "
        "closed at $1.24M, below the planned $1.48M."
    )
    assert [c["metric"] for c in multi] == ["opex_total", "operating_income"]
    assert [c["direction"] for c in multi] == ["up", "down"]


def test_prose_comparator_must_match_section_scope() -> None:
    # Every claim in the clean commentary names the comparator its section covers.
    for section in STRICT["sections"]:
        for paragraph in section["paragraphs"]:
            text = "".join(
                token["v"] if token["t"] == "text" else token["text"]
                for token in paragraph["tokens"]
            )
            for claim in verify_engine.extract_direction_claims(text):
                assert claim["comparator"] == section["scope"]["comparator"]

    variance = {
        "variance_id": "V-999",
        "variance_usd": "167849.10",
        "favorability": "favorable",
        "account_short": "Revenue",
    }
    wrong = _claim_categories(
        "Revenue came in at $4.56M, above the revised plan of $4.39M by $167.8K.", variance
    )
    assert "COMPARATOR_MISMATCH" in wrong
    right = _claim_categories(
        "Revenue came in at $4.56M, above the plan of $4.39M by $167.8K.", variance
    )
    assert right == set()


def test_direction_semantics_handle_zero_and_inline() -> None:
    zero = {
        "variance_id": "V-998",
        "variance_usd": "0.00",
        "favorability": "neutral",
        "account_short": "Cash",
    }
    # A hard-zero variance cannot be described as a movement in either direction.
    assert "DIRECTION_CONFLICT" in _claim_categories(
        "Cash came in at $0.00, above the plan by $0.00.", zero
    )
    assert "DIRECTION_CONFLICT" in _claim_categories(
        "Cash closed at $0.00, below plan.", zero
    )
    # "In line with plan" is the correct description of zero.
    assert _claim_categories("Cash came in at $0.00, in line with plan.", zero) == set()

    # And a real movement cannot hide behind "in line".
    nonzero = {**zero, "variance_usd": "150000.00"}
    assert "DIRECTION_CONFLICT" in _claim_categories(
        "Cash came in at $150.0K, in line with plan.", nonzero
    )
    assert _claim_categories("Cash came in at $150.0K, above plan.", nonzero) == set()


def test_cross_section_driver_totals_reconcile() -> None:
    cross = VERIFIED["cross_section"]
    assert cross["total"] == cross["passed"] > 0
    assert len(cross["driver_totals"]) == len({r["driver"] for r in cross["driver_totals"]}) == 10
    for row in cross["driver_totals"]:
        assert row["status"] == "pass"
        assert row["expected_usd"] == row["cited_usd"]
        assert row["difference_usd"] == "0.00"
    # Every live prose claim (primary cause, complete attribution, entire
    # movement) ties to the arithmetic behind it.
    assert all(claim["status"] == "pass" for claim in cross["claims"])
    assert {claim["claim"] for claim in cross["claims"]} >= {"primary driver", "attribution complete"}


def _cross_categories(sections: list[dict]) -> tuple[set[str], dict]:
    findings = verify_engine.Findings()
    facts = {f["fact_id"]: f for f in RECON["facts"]}
    result = verify_engine._check_cross_section(sections, facts, RECON, findings)
    return findings.categories(), result


def test_driver_told_in_two_sections_blocks() -> None:
    sections = copy.deepcopy(STRICT["sections"])
    with_drivers = [s for s in sections if s["kind"] == "variance" and s.get("driver_fact_ids")]
    with_drivers[1]["driver_fact_ids"].append(with_drivers[0]["driver_fact_ids"][0])
    categories, result = _cross_categories(sections)
    assert {"DRIVER_DOUBLE_COUNT", "DRIVER_TOTAL_MISMATCH"} <= categories
    assert any(row["status"] == "fail" for row in result["driver_totals"])
    assert any(row["difference_usd"] != "0.00" for row in result["driver_totals"])


def test_primary_driver_must_be_the_largest_attribution() -> None:
    sections = copy.deepcopy(STRICT["sections"])
    by_var = {v["variance_id"]: v for v in RECON["variances"]}
    target = next(s for s in sections if s["kind"] == "variance")
    variance = by_var[target["variance_id"]]
    nonzero = [d for d in variance["drivers"] if Decimal(d["amount"]) != 0]
    assert len(nonzero) >= 2
    primary, other = nonzero[0], nonzero[1]
    replaced = False
    for paragraph in target["paragraphs"]:
        for token in paragraph["tokens"]:
            if token["t"] == "text" and primary["cause"] in token["v"]:
                token["v"] = token["v"].replace(primary["cause"], other["cause"], 1)
                replaced = True
    assert replaced
    categories, result = _cross_categories(sections)
    assert categories == {"PRIMARY_DRIVER_CONFLICT"}
    assert any(claim["claim"] == "primary driver" and claim["status"] == "fail"
               for claim in result["claims"])


def test_summary_cannot_claim_coverage_the_sections_do_not_have() -> None:
    sections = copy.deepcopy(STRICT["sections"])
    summary = next(s for s in sections if s["kind"] == "summary")
    summary["paragraphs"][-1]["tokens"].append({
        "t": "text",
        "v": " The gap to plan at group level is fully explained by the entity variances set out below.",
    })
    categories, result = _cross_categories(sections)
    assert categories == {"COVERAGE_OVERSTATED"}
    claim = next(c for c in result["claims"] if c["claim"] == "group gap fully explained")
    assert claim["status"] == "fail"
    assert Decimal(claim["actual"]) != Decimal(claim["expected"])


def test_drift_is_blocked() -> None:
    gate = DRIFT_VERIFIED["gate"]
    assert gate["status"] == "blocked"
    assert gate["blocking"] == 3
    assert gate["signable"] is False
    blocks = [f for f in DRIFT_VERIFIED["findings"] if f["severity"] == "block"]
    categories = {f["category"] for f in blocks}
    assert categories == {"BASIS_MISMATCH", "UNSOURCED_FIGURE", "VALUE_MISMATCH"}
    assert DRIFT_VERIFIED["stats"]["figures_passed"] < DRIFT_VERIFIED["stats"]["figures_checked"]


# ----------------------------------------------------------- sign-off
def _attestation_for(finding: dict) -> dict:
    """A template-complete attestation for one warning."""
    details = {
        "remediation_owner": "R. Tan, APAC Controller",
        "remediation_date": "2026-12-31",
    }
    if finding["category"] == "CONTROL_BREAK":
        details["control_id"] = finding["control_id"]
    if finding["category"] == "DRIVER_UNDISCLOSED":
        details["reviewed_by"] = "Group FP&A lead"
    return {
        "finding_id": finding["finding_id"],
        "rationale": "Accepted as immaterial; cleared after cut-off.",
        "details": details,
    }


def _attestations(verification: dict) -> list[dict]:
    return [
        _attestation_for(f)
        for f in verification["findings"]
        if f["severity"] == "warn"
    ]


def _sign(run_id: str = "run_test", prev: str = "genesis", sequence: int = 1,
          verification: dict | None = None, commentary: dict | None = None,
          signer: str = "A. Mensah", preparer: str = "J. Okafor",
          dual_control: bool = False) -> dict:
    return audit.sign(
        {"id": run_id},
        RECON,
        commentary or STRICT,
        verification or VERIFIED,
        signer=signer,
        role="Group Controller",
        note="October close reviewed.",
        attestations=_attestations(verification or VERIFIED),
        prev_chain_hash=prev,
        sequence=sequence,
        preparer=preparer,
        dual_control=dual_control,
    )


def test_signoff_requires_attestation() -> None:
    try:
        audit.sign(
            {"id": "run_test"}, RECON, STRICT, VERIFIED,
            signer="A. Mensah", role="Group Controller", note="",
            attestations=[], prev_chain_hash="genesis", sequence=1,
            preparer="J. Okafor",
        )
    except audit.SignOffRefused as exc:
        assert "attested" in str(exc)
    else:
        raise AssertionError("sign-off without attestations was accepted")


def test_signoff_requires_named_signer() -> None:
    try:
        audit.sign(
            {"id": "run_test"}, RECON, STRICT, VERIFIED,
            signer="   ", role="Group Controller", note="",
            attestations=_attestations(VERIFIED), prev_chain_hash="genesis", sequence=1,
            preparer="J. Okafor",
        )
    except audit.SignOffRefused:
        return
    raise AssertionError("sign-off without a signer was accepted")


def test_separation_of_duties_requires_a_different_signer() -> None:
    # The preparer cannot sign their own run.
    try:
        _sign(signer="J. Okafor", preparer="j. okafor")
    except audit.SignOffRefused as exc:
        assert "Separation of duties" in str(exc)
    else:
        raise AssertionError("the preparer signed their own run")

    # A missing preparer is refused rather than silently waived.
    try:
        _sign(preparer="   ")
    except audit.SignOffRefused as exc:
        assert "preparer" in str(exc)
    else:
        raise AssertionError("a sign-off with no preparer of record was accepted")

    # Dual control waives the conflict and the waiver is bound into the record.
    record = _sign(signer="J. Okafor", preparer="J. Okafor", dual_control=True)
    assert record["signer"] == "J. Okafor"
    assert record["preparer"] == "J. Okafor"
    assert record["dual_control"] is True
    assert record["chain_hash"] == audit.digest(audit._chain_payload(record))

    # A different signer is accepted without the waiver.
    record = _sign(signer="A. Mensah", preparer="J. Okafor")
    assert record["preparer"] == "J. Okafor"
    assert record["dual_control"] is False


def test_chain_links_and_detects_tamper() -> None:
    first = _sign("run_a", "genesis", 1)
    second = _sign("run_b", first["chain_hash"], 2)
    runs = {
        "run_a": {"payload": {"recon": RECON, "commentary": STRICT, "verification": VERIFIED}},
        "run_b": {"payload": {"recon": RECON, "commentary": STRICT, "verification": VERIFIED}},
    }
    chain = audit.verify_chain([first, second], runs)
    assert [c["status"] for c in chain] == ["intact", "intact"]
    assert [c["sequence"] for c in chain] == [1, 2]
    assert chain[1]["link_ok"] and chain[1]["hash_ok"] and chain[1]["sequence_ok"] and chain[1]["content_ok"]

    tampered = json.loads(json.dumps(STRICT))
    tampered["sections"][0]["paragraphs"][0]["tokens"][0]["v"] += " (edited)"
    runs["run_a"] = {"payload": {"recon": RECON, "commentary": tampered, "verification": VERIFIED}}
    chain = audit.verify_chain([first, second], runs)
    assert chain[0]["status"] == "broken" and chain[0]["content_ok"] is False
    # Re-linking the next record cannot hide the edit either.
    assert chain[1]["link_ok"] is True

    broken = json.loads(json.dumps(second))
    broken["chain_hash"] = "0" * 64
    assert audit.verify_chain([first, broken], runs)[1]["hash_ok"] is False


def test_chain_order_is_sequence_not_wall_clock() -> None:
    first = _sign("run_a", "genesis", 1)
    second = _sign("run_b", first["chain_hash"], 2)

    # Reversed input order must not reverse the chain.
    reversed_chain = audit.verify_chain([second, first], {})
    assert [c["signoff_id"] for c in reversed_chain] == ["SO-0001", "SO-0002"]
    assert [c["status"] for c in reversed_chain] == ["intact", "intact"]

    # Two signoffs in the same second are still ordered by sequence.
    same_a = copy.deepcopy(first)
    same_b = copy.deepcopy(second)
    same_a["signed_at"] = same_b["signed_at"] = "2026-11-14T10:00:00+00:00"
    ordered = audit.verify_chain([same_b, same_a], {})
    assert [c["signoff_id"] for c in ordered] == ["SO-0001", "SO-0002"]

    # Timestamp tampering cannot move a record ahead of its predecessor.
    swapped_a = copy.deepcopy(first)
    swapped_b = copy.deepcopy(second)
    swapped_a["signed_at"] = "2026-11-14T10:00:02+00:00"
    swapped_b["signed_at"] = "2026-11-14T10:00:01+00:00"
    ordered = audit.verify_chain([swapped_b, swapped_a], {})
    assert [c["signoff_id"] for c in ordered] == ["SO-0001", "SO-0002"]
    assert all(c["hash_ok"] is False for c in ordered)
    assert all(c["status"] == "broken" for c in ordered)

    # A rewritten sequence changes both the committed payload and the order.
    tampered = copy.deepcopy(second)
    tampered["sequence"] = 1
    tampered["signoff_id"] = "SO-0001"
    result = audit.verify_chain([first, tampered], {})
    assert result[1]["hash_ok"] is False
    assert result[1]["sequence_ok"] is False
    assert result[1]["status"] == "broken"


def test_legacy_signoff_without_sequence_still_verifies() -> None:
    record = _sign("run_a", "genesis", 1)
    legacy = copy.deepcopy(record)
    legacy.pop("sequence")
    # Records from before separation of duties also predate the preparer fields.
    legacy.pop("preparer", None)
    legacy.pop("dual_control", None)
    # ...and predate the engine/verifier version stamps.
    legacy.pop("engine_version", None)
    legacy.pop("verifier_version", None)
    legacy["chain_hash"] = audit.digest({
        "prev": legacy["prev_chain_hash"],
        "run_id": legacy["run_id"],
        "content": legacy["hashes"]["content_hash"],
        "verification": legacy["hashes"]["verification_hash"],
        "facts": legacy["hashes"]["fact_store_hash"],
        "signer": legacy["signer"],
        "signed_at": legacy["signed_at"],
    })
    result = audit.verify_chain([legacy], {})
    assert result[0]["sequence"] == 1
    assert result[0]["sequence_ok"] is True
    assert result[0]["status"] == "intact"


def test_store_loads_signoffs_in_sequence_order() -> None:
    original_path = store.DB_PATH
    store.DB_PATH = str(_TMP / "sequence-order.db")
    try:
        first = _sign("run_a", "genesis", 1)
        second = _sign("run_b", first["chain_hash"], 2)
        # Insert in reverse order, both stamped with the same second.
        for record in (second, first):
            record = copy.deepcopy(record)
            record["signed_at"] = "2026-11-14T10:00:00+00:00"
            store.save_signoff(record)
        loaded = store.load_signoffs()
        assert [r["signoff_id"] for r in loaded] == ["SO-0001", "SO-0002"]
        assert store.next_sequence() == 3
    finally:
        store.DB_PATH = original_path


def test_fact_hash_commits_to_full_derivation() -> None:
    base = audit.hashes(RECON, STRICT, VERIFIED)
    probes = []

    def probe(ftype: str):
        fact = next(f for f in RECON["facts"] if f["derivation"]["type"] == ftype)
        probes.append(fact)

    for kind in ("ledger_sum", "bs_closing", "plan_lookup", "driver_sum", "aggregate"):
        probe(kind)

    mutations = [
        lambda f: f["derivation"]["lines"][0].__setitem__("je_id", "JE-FORGED"),
        lambda f: f["derivation"].__setitem__("opening_period", "2026-01"),
        lambda f: f["derivation"].__setitem__("rate", "9.9999"),
        lambda f: f["derivation"].__setitem__("driver", "forged-driver"),
        lambda f: f["derivation"]["accounts"].append(["9999", 1]),
    ]
    for fact, mutate in zip(probes, mutations):
        tampered = copy.deepcopy(RECON)
        target = next(f for f in tampered["facts"] if f["fact_id"] == fact["fact_id"])
        mutate(target)
        assert target["value"] == fact["value"], "probe must leave the reported value untouched"
        changed = audit.hashes(tampered, STRICT, VERIFIED)
        assert changed["fact_store_hash"] != base["fact_store_hash"], fact["derivation"]["type"]
        assert changed["content_hash"] == base["content_hash"]
        assert changed["verification_hash"] == base["verification_hash"]

    # Source references are provenance too.
    tampered = copy.deepcopy(RECON)
    tampered["facts"][0]["source"]["ref"] = "forged reference"
    assert audit.hashes(tampered, STRICT, VERIFIED)["fact_store_hash"] != base["fact_store_hash"]


def test_chain_detects_derivation_edit() -> None:
    record = _sign()
    tampered = copy.deepcopy(RECON)
    fact = next(f for f in tampered["facts"] if f["derivation"]["type"] == "ledger_sum")
    fact["derivation"]["lines"][0]["je_id"] = "JE-FORGED"
    chain = audit.verify_chain([record], {
        "run_test": {"payload": {"recon": tampered, "commentary": STRICT, "verification": VERIFIED}},
    })
    assert chain[0]["hash_ok"] is True          # the signature record itself is untouched
    assert chain[0]["content_ok"] is False      # but the live evidence no longer matches
    assert chain[0]["status"] == "broken"


def test_evidence_pack_manifest_matches_signature() -> None:
    record = _sign()
    pack = audit.evidence_pack(
        {"id": "run_test"}, RECON, STRICT, VERIFIED, record,
        audit.verify_chain([record], {"run_test": {"payload": {"recon": RECON, "commentary": STRICT, "verification": VERIFIED}}}),
    )
    for key in ("content_hash", "verification_hash", "fact_store_hash", "chain_hash"):
        assert pack["manifest"][key] == record["hashes"].get(key) or pack["manifest"][key] == record.get(key)
    assert pack["manifest"]["signoff_id"] == record["signoff_id"]
    assert pack["manifest"]["signed"] is True
    assert pack["pack_id"] == pack["manifest"]["pack_id"]
    assert audit.verify_pack_hash(pack)
    assert pack["engagement"]["group"] == "Acme Global Holdings"
    assert pack["engagement"]["engine_version"] == config.ENGINE_VERSION
    assert pack["engagement"]["verifier_version"] == config.VERIFIER_VERSION


def test_evidence_pack_id_is_stable_and_hash_is_recomputable() -> None:
    record = _sign()
    runs = {"run_test": {"payload": {"recon": RECON, "commentary": STRICT, "verification": VERIFIED}}}
    chain = audit.verify_chain([record], runs)
    first = audit.evidence_pack({"id": "run_test"}, RECON, STRICT, VERIFIED, record, chain)
    second = audit.evidence_pack({"id": "run_test"}, RECON, STRICT, VERIFIED, record, chain)

    # The identity of the evidence does not move between downloads.
    assert first["pack_id"] == second["pack_id"]
    assert first["manifest"]["pack_id"] == first["pack_id"]
    # Each download carries its own digest, and that digest verifies.
    assert audit.verify_pack_hash(first)
    assert audit.verify_pack_hash(second)
    tampered = json.loads(json.dumps(first))
    tampered["variances"][0]["variance_usd"] = "0.01"
    assert audit.verify_pack_hash(tampered) is False

    # An unsigned pack also has a stable id derived from the live artefact hashes.
    unsigned_a = audit.evidence_pack({"id": "run_test"}, RECON, STRICT, VERIFIED, None, [])
    unsigned_b = audit.evidence_pack({"id": "run_test"}, RECON, STRICT, VERIFIED, None, [])
    assert unsigned_a["pack_id"] == unsigned_b["pack_id"]
    assert unsigned_a["manifest"]["signed"] is False


def test_version_stamps_reach_recon_verification_and_signoff() -> None:
    assert RECON["engine_version"] == config.ENGINE_VERSION
    assert STRICT["engine_version"] == config.ENGINE_VERSION
    assert VERIFIED["engine_version"] == config.ENGINE_VERSION
    assert VERIFIED["verifier_version"] == config.VERIFIER_VERSION

    record = _sign()
    assert record["engine_version"] == config.ENGINE_VERSION
    assert record["verifier_version"] == config.VERIFIER_VERSION
    # Versions are part of the chain payload, so a change is visible.
    payload = audit._chain_payload(record)
    assert payload["engine_version"] == config.ENGINE_VERSION
    assert payload["verifier_version"] == config.VERIFIER_VERSION
    assert record["chain_hash"] == audit.digest(payload)


def test_control_break_attestation_requires_remediation_details() -> None:
    warning = next(f for f in VERIFIED["findings"] if f["category"] == "CONTROL_BREAK")
    prose_only = {"finding_id": warning["finding_id"], "rationale": "Accepted."}
    try:
        audit.sign(
            {"id": "run_test"}, RECON, STRICT, VERIFIED,
            signer="A. Mensah", role="Group Controller", note="",
            attestations=[prose_only], prev_chain_hash="genesis", sequence=1,
            preparer="J. Okafor",
        )
    except audit.SignOffRefused as exc:
        assert "Control reference" in str(exc) or "Remediation" in str(exc)
    else:
        raise AssertionError("a prose-only control-break attestation was accepted")

    # The control reference must be the one the finding raised, not a guess.
    wrong = copy.deepcopy(_attestation_for(warning))
    wrong["details"]["control_id"] = "TB-OTHER"
    try:
        audit.sign(
            {"id": "run_test"}, RECON, STRICT, VERIFIED,
            signer="A. Mensah", role="Group Controller", note="",
            attestations=[wrong], prev_chain_hash="genesis", sequence=1,
            preparer="J. Okafor",
        )
    except audit.SignOffRefused as exc:
        assert "Control reference" in str(exc)
    else:
        raise AssertionError("a mismatched control reference was accepted")

    record = _sign()
    structured = next(a for a in record["attestations"] if a["category"] == "CONTROL_BREAK")
    assert structured["template"] == config.ATTESTATION_TEMPLATES["CONTROL_BREAK"]["label"]
    assert structured["control_id"] == warning["control_id"]
    assert structured["remediation_owner"]
    assert structured["remediation_date"] == "2026-12-31"
    assert structured["details"]["control_id"] == warning["control_id"]


def test_diff_reports_changes_and_invalidates_a_signature() -> None:
    controller = _client("controller")
    base = _post(controller, "/api/runs", {"mode": "strict"}).json()
    base_id = base["run"]["id"]
    signer = _client("signer")
    signed = _post(signer, f"/api/runs/{base_id}/signoff", {
        "attestations": _attestations(base["verification"]),
    })
    assert signed.status_code == 201, signed.text

    # A clean re-run of the same period: identical artefacts, signature intact.
    same = _post(controller, "/api/runs", {"mode": "strict"}).json()
    same_diff = controller.get(f"/api/runs/{base_id}/diff/{same['run']['id']}").json()
    assert same_diff["same_period"] is True
    assert same_diff["summary"]["facts_changed"] == 0
    assert same_diff["summary"]["findings_added"] == 0
    assert same_diff["summary"]["sections_changed"] == 0
    assert same_diff["hashes"]["match"] is True
    assert same_diff["signoffs"]["invalidated"] is False
    assert same_diff["versions"]["engine_changed"] is False
    assert same_diff["versions"]["verifier_changed"] is False

    # An adversarial re-narration changes the figures and raises findings.
    drift = _post(controller, "/api/runs", {"mode": "drift"}).json()
    diff = controller.get(f"/api/runs/{base_id}/diff/{drift['run']['id']}").json()
    assert diff["summary"]["sections_changed"] >= 1
    assert diff["summary"]["findings_added"] >= 3
    assert any(f["category"] == "UNSOURCED_FIGURE" for f in diff["findings"]["added"])
    assert diff["hashes"]["match"] is False
    assert diff["signoffs"]["invalidated"] is True
    assert "base signature does not cover" in diff["signoffs"]["reason"]
    assert diff["base"]["signoff_id"] == signed.json()["signoff"]["signoff_id"]

    # Every reported figure change carries both values and a delta.
    changed_sections = diff["sections"]["changed"]
    assert all(set(s["reasons"]) for s in changed_sections)


def test_rollforward_pivots_four_periods_with_the_current_last() -> None:
    client = _client("auditor")
    pivot = client.get("/api/rollforward").json()
    assert pivot["periods"] == config.PERIODS
    assert pivot["periods"][-1] == pivot["current_period"]
    assert pivot["stats"]["periods"] == 4

    group_revenue = next(
        row for row in pivot["rows"] if row["entity"] == "GRP" and row["account"] == "4000"
    )
    for period in config.PERIODS:
        cell = group_revenue["periods"][period]
        variance = Decimal(cell["actual_usd"]) - Decimal(cell["budget_usd"])
        assert variance == Decimal(cell["variance_usd"])

    # Group materiality is recomputed at group level, not inherited from entities.
    group_rows = [r for r in pivot["rows"] if r["group"]]
    entity_rows = [r for r in pivot["rows"] if not r["group"]]
    assert group_rows and entity_rows
    assert all(len(r["periods"]) >= 3 for r in group_rows)

    filtered = client.get("/api/rollforward", params={"entity": "US01", "statement": "PL"}).json()
    assert {r["entity"] for r in filtered["rows"]} == {"US01"}
    assert {r["statement"] for r in filtered["rows"]} == {"PL"}
    assert filtered["filter"]["entity"] == "US01"


# ------------------------------------------------------------- HTTP API
def _client(identity: str = "preparer"):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.post("/auth/dev/login", json={"identity": identity})
    assert response.status_code == 200, response.text
    return client


def _login(client, identity: str) -> dict:
    response = client.post("/auth/dev/login", json={"identity": identity})
    assert response.status_code == 200, response.text
    return response.json()


def _csrf(client) -> str:
    state = client.get("/api/auth/me").json()
    assert state["authenticated"] is True, state
    token = state["csrf_token"]
    assert token, state
    return token


def _headers(client, extra: dict | None = None) -> dict:
    headers = {"X-CSRF-Token": _csrf(client)}
    if extra:
        headers.update(extra)
    return headers


def _post(client, path: str, body=None, **kwargs):
    return client.post(path, json=body, headers=_headers(client), **kwargs)


def _delete(client, path: str):
    return client.delete(path, headers=_headers(client))


def test_http_requires_authentication_and_uses_one_error_envelope() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    anon = TestClient(app)
    response = anon.get("/api/runs")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "authentication_required"
    assert error["request_id"]
    assert response.headers["X-Request-ID"] == error["request_id"]

    # Liveness, readiness and auth state are public; financial routes are not.
    assert anon.get("/api/health").json()["ok"] is True
    assert anon.get("/api/ready").status_code == 200
    state = anon.get("/api/auth/me").json()
    assert state["authenticated"] is False
    assert state["auth_mode"] == "dev"
    assert {item["identity"] for item in state["dev_identities"]} >= {
        "preparer", "signer", "admin",
    }
    assert anon.get("/api/meta").status_code == 401

    shell = anon.get("/")
    assert shell.status_code == 200
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
        "Permissions-Policy",
    ):
        assert shell.headers.get(header), header
    assert shell.headers["Cache-Control"] == "no-store"

    # Documentation is available in the test profile and can be disabled.
    assert anon.get("/openapi.json").status_code == 200

    # A disallowed Host header is refused before routing.
    assert anon.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_http_pipeline() -> None:
    preparer = _client("preparer")

    assert preparer.get("/api/health").json()["ok"] is True
    meta = preparer.get("/api/meta").json()
    assert meta["group"] == "Acme Global Holdings"
    assert meta["auth"]["mode"] == "dev"

    created = _post(preparer, "/api/runs", {"mode": "strict"})
    assert created.status_code == 201, created.text
    view = created.json()
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "attestation"
    assert view["run"]["preparer"] == "J. Okafor"
    assert view["run"]["preparer_subject"] == "dev:preparer"
    assert view["viewer"]["subject"] == "dev:preparer"
    assert view["viewer"]["can_sign"] is False  # preparer has no signer role
    assert view["verification"]["gate"]["blocking"] == 0
    assert preparer.get(f"/api/runs/{run_id}").status_code == 200
    assert preparer.get("/api/runs/run_missing").status_code == 404

    fact_id = view["recon"]["facts"][0]["fact_id"]
    fact = preparer.get(f"/api/runs/{run_id}/facts/{fact_id}").json()
    assert fact["check"]["status"] == "pass"

    # A preparer cannot sign without the signer role.
    assert _post(preparer, f"/api/runs/{run_id}/signoff", {
        "attestations": [],
    }).status_code == 403

    signer = _client("signer")
    warn_ids = [f["finding_id"] for f in view["verification"]["findings"] if f["severity"] == "warn"]
    refused = _post(signer, f"/api/runs/{run_id}/signoff", {"attestations": []})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "signoff_refused"
    assert "attested" in refused.json()["error"]["message"]

    signed = _post(signer, f"/api/runs/{run_id}/signoff", {
        "note": "Reviewed with the CFO.",
        "attestations": _attestations(view["verification"]),
    })
    assert signed.status_code == 201, signed.text
    signed_view = signed.json()
    assert signed_view["run"]["status"] == "signed"
    assert signed_view["viewer"]["can_sign"] is False
    assert signed_view["signoff"]["signer"] == "A. Mensah"
    assert signed_view["signoff"]["signer_subject"] == "dev:signer"
    assert signed_view["signoff"]["preparer_subject"] == "dev:preparer"
    assert signed_view["signoff"]["dual_control"] is False
    assert signed_view["signoff"]["counts"]["warnings_attested"] == len(warn_ids)
    assert signed_view["chain"][-1]["status"] == "intact"

    # A signed record is frozen: mutation routes refuse it, and a second
    # signature is impossible even from the same signer.
    assert _post(preparer, f"/api/runs/{run_id}/renarrate", {"mode": "strict"}).status_code == 409
    assert _post(preparer, f"/api/runs/{run_id}/verify").status_code == 409
    assert _post(signer, f"/api/runs/{run_id}/signoff", {"attestations": []}).status_code == 409

    exported = signer.get(f"/api/runs/{run_id}/export")
    assert exported.status_code == 200
    assert exported.headers["X-Pack-ID"]
    pack = exported.json()
    assert pack["manifest"]["content_hash"] == signed_view["signoff"]["hashes"]["content_hash"]
    assert pack["manifest"]["verification_hash"] == signed_view["signoff"]["hashes"]["verification_hash"]
    assert pack["manifest"]["fact_store_hash"] == signed_view["signoff"]["hashes"]["fact_store_hash"]
    assert pack["manifest"]["signoff_id"] == signed_view["signoff"]["signoff_id"]
    assert pack["pack_id"] == pack["manifest"]["pack_id"]
    assert audit.verify_pack_hash(pack)
    assert pack["chain"][-1]["status"] == "intact"

    # The stable id survives a second download; the per-download digest still verifies.
    again = signer.get(f"/api/runs/{run_id}/export").json()
    assert again["pack_id"] == pack["pack_id"]
    assert audit.verify_pack_hash(again)

    csv = signer.get(f"/api/runs/{run_id}/export.csv")
    assert csv.status_code == 200
    assert csv.text.splitlines()[0].startswith("section_id,variance_id")
    assert len(csv.text.splitlines()) == 14  # header + 13 sections

    chain = signer.get("/api/audit/chain").json()
    assert chain["intact"] is True
    assert chain["signoffs"][-1]["signoff_id"] == signed_view["signoff"]["signoff_id"]

    # Exports are an auditable read: who exported which pack.
    events = signer.get(f"/api/runs/{run_id}").json()["events"]
    export_events = [e for e in events if e["action"] == "audit.exported"]
    assert len(export_events) >= 2
    pack_events = [e for e in export_events if e["detail"].get("format") == "evidence_pack"]
    assert pack_events
    assert pack_events[-1]["detail"]["actor_subject"] == "dev:signer"
    assert pack_events[-1]["detail"]["pack_hash"]


def test_http_csrf_and_logout() -> None:
    client = _client("preparer")

    missing = client.post("/api/runs", json={"mode": "strict"})
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_failed"

    wrong = client.post(
        "/api/runs", json={"mode": "strict"}, headers={"X-CSRF-Token": "not-the-token"}
    )
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "csrf_failed"

    good = _post(client, "/api/runs", {"mode": "strict"})
    assert good.status_code == 201

    logout = _post(client, "/auth/logout")
    assert logout.status_code == 200 and logout.json()["ok"] is True
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_http_role_based_access_control() -> None:
    preparer = _client("preparer")
    run = _post(preparer, "/api/runs", {"mode": "strict"}).json()
    run_id = run["run"]["id"]

    reviewer = _client("reviewer")
    assert reviewer.get("/api/runs").status_code == 200
    assert reviewer.get(f"/api/runs/{run_id}").status_code == 200
    assert _post(reviewer, "/api/runs", {"mode": "strict"}).status_code == 403
    assert _post(reviewer, f"/api/runs/{run_id}/renarrate", {"mode": "strict"}).status_code == 403
    assert _post(reviewer, f"/api/runs/{run_id}/signoff", {"attestations": []}).status_code == 403
    assert reviewer.get(f"/api/runs/{run_id}/export").status_code == 403

    auditor = _client("auditor")
    assert auditor.get(f"/api/runs/{run_id}").status_code == 200
    assert auditor.get("/api/audit/chain").status_code == 200
    assert auditor.get(f"/api/runs/{run_id}/export").status_code == 200
    assert _post(auditor, "/api/runs", {"mode": "strict"}).status_code == 403
    assert _post(auditor, f"/api/runs/{run_id}/verify").status_code == 403
    assert _post(auditor, f"/api/runs/{run_id}/signoff", {"attestations": []}).status_code == 403

    # Admin is authorized for policy but the account is not a bypass around
    # subject-based separation of duties (covered in the next test).
    admin = _client("admin")
    assert admin.get("/api/audit/chain").status_code == 200
    assert _post(admin, f"/api/runs/{run_id}/dual-control", {
        "reason": "Independent controller approval for coverage during month-end.",
    }).status_code == 201


def test_http_separation_of_duties_and_admin_dual_control() -> None:
    from datetime import datetime, timedelta, timezone

    controller = _client("controller")
    created = _post(controller, "/api/runs", {"mode": "strict"})
    assert created.status_code == 201, created.text
    view = created.json()
    run_id = view["run"]["id"]
    assert view["run"]["preparer_subject"] == "dev:controller"
    attestations = _attestations(view["verification"])

    refused = _post(controller, f"/api/runs/{run_id}/signoff", {
        "attestations": attestations,
    })
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "separation_of_duties"

    # The preparer cannot approve their own exception, and a non-admin cannot
    # approve one either.
    assert _post(controller, f"/api/runs/{run_id}/dual-control", {
        "reason": "The preparer should not be able to waive their own control.",
    }).status_code == 403

    admin = _client("admin")
    expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    approved = _post(admin, f"/api/runs/{run_id}/dual-control", {
        "reason": "Independent controller approval: sole preparer during close week.",
        "expires_at": expires,
    })
    assert approved.status_code == 201, approved.text
    approval = approved.json()["approval"]
    assert approval["approved_subject"] == "dev:admin"
    assert approval["requested_subject"] == "dev:controller"

    signed = _post(controller, f"/api/runs/{run_id}/signoff", {
        "note": "Signed under an approved dual-control exception.",
        "attestations": attestations,
    })
    assert signed.status_code == 201, signed.text
    record = signed.json()["signoff"]
    assert record["signer_subject"] == record["preparer_subject"] == "dev:controller"
    assert record["dual_control"] is True
    assert record["dual_control_approval"]["approval_id"] == approval["approval_id"]
    assert record["dual_control_approval"]["approved_subject"] == "dev:admin"
    assert signed.json()["chain"][-1]["status"] == "intact"

    # A revoked approval no longer permits a self-signature.
    second = _post(controller, "/api/runs", {"mode": "strict"}).json()
    second_id = second["run"]["id"]
    assert _post(admin, f"/api/runs/{second_id}/dual-control", {
        "reason": "Temporary approval to be revoked before signature.",
    }).status_code == 201
    assert _delete(admin, f"/api/runs/{second_id}/dual-control").status_code == 200
    assert _post(controller, f"/api/runs/{second_id}/signoff", {
        "attestations": _attestations(second["verification"]),
    }).status_code == 409

    # An admin cannot approve an exception on a run they prepared themselves.
    own = _post(admin, "/api/runs", {"mode": "strict"}).json()
    assert _post(admin, f"/api/runs/{own['run']['id']}/dual-control", {
        "reason": "An approver may not also be the preparer of the same run.",
    }).status_code == 409


def test_http_validation_bounds_and_enum_values() -> None:
    client = _client("preparer")

    bad_period = _post(client, "/api/runs", {"mode": "strict", "period": "2026-99"})
    assert bad_period.status_code == 422
    assert bad_period.json()["error"]["code"] == "validation_error"
    assert "period" in bad_period.json()["error"]["message"]

    bad_mode = _post(client, "/api/runs", {"mode": "creative"})
    assert bad_mode.status_code == 422

    # Identity fields were removed from the public request schema.
    legacy_identity = _post(client, "/api/runs", {
        "mode": "strict", "prepared_by": "Someone Else", "dual_control": True,
    })
    assert legacy_identity.status_code == 422

    run = _post(client, "/api/runs", {"mode": "strict"}).json()
    run_id = run["run"]["id"]
    signer = _client("signer")
    legacy_signer = _post(signer, f"/api/runs/{run_id}/signoff", {
        "signer": "A. Mensah", "attestations": [],
    })
    assert legacy_signer.status_code == 422
    assert "signer" in legacy_signer.json()["error"]["message"] or legacy_signer.json()["error"].get("details")

    overlong = _post(signer, f"/api/runs/{run_id}/signoff", {
        "note": "x" * 2001,
        "attestations": [],
    })
    assert overlong.status_code == 422

    assert client.get("/api/rollforward", params={"entity": "NOPE"}).status_code == 422
    assert client.get("/api/rollforward", params={"statement": "XX"}).status_code == 422


def test_http_dev_header_identity_is_local_machine_only() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    auditor_headers = {"X-Dev-Subject": "dev:auditor", "X-Dev-Roles": "auditor"}
    assert client.get("/api/runs", headers=auditor_headers).status_code == 200
    assert client.post(
        "/api/runs", json={"mode": "strict"}, headers=auditor_headers
    ).status_code == 403

    builder_headers = {"X-Dev-Subject": "dev:controller", "X-Dev-Roles": "preparer,signer"}
    created = client.post("/api/runs", json={"mode": "strict"}, headers=builder_headers)
    assert created.status_code == 201, created.text
    assert created.json()["run"]["preparer_subject"] == "dev:controller"


def test_http_body_limit_and_rate_limit_are_enforced() -> None:
    import app.security as security

    original = security.settings
    client = _client("preparer")
    try:
        security.settings = original.with_overrides(max_body_bytes=2048)
        response = client.post(
            "/api/runs",
            json={"mode": "strict", "period": "2026-10", "padding": "x" * 4000},
            headers=_headers(client),
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
    finally:
        security.settings = original

    client = _client("preparer")
    security.limiter.reset()
    try:
        security.settings = original.with_overrides(rate_limit_read_per_minute=2)
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/meta").status_code == 200
        limited = client.get("/api/meta")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert limited.headers.get("Retry-After")
    finally:
        security.settings = original
        security.limiter.reset()


def test_readiness_reports_each_dependency() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.get("/api/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert set(body["checks"]) >= {"database", "dataset", "dataset_contract", "audit_chain"}
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["audit_chain"]["ok"] is True


def test_production_settings_fail_closed() -> None:
    from app.settings import ConfigurationError, Settings

    valid = {
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "SESSION_SECRET": "s" * 40,
        "ALLOWED_HOSTS": "close.example.com",
        "PUBLIC_ORIGIN": "https://close.example.com",
        "OIDC_ISSUER": "https://login.example.com",
        "OIDC_CLIENT_ID": "ledgerline",
        "OIDC_CLIENT_SECRET": "client-secret",
        "OIDC_REDIRECT_URI": "https://close.example.com/auth/callback",
    }

    def refused(env: dict) -> None:
        try:
            Settings.from_env(env)
        except ConfigurationError:
            return
        raise AssertionError(f"configuration should have been refused: {sorted(env)}")

    refused({**valid, "AUTH_MODE": "dev"})
    refused({k: v for k, v in valid.items() if k != "SESSION_SECRET"})
    refused({**valid, "ALLOWED_HOSTS": "*"})
    refused({k: v for k, v in valid.items() if k != "PUBLIC_ORIGIN"})
    refused({**valid, "OIDC_CLIENT_SECRET": ""})
    refused({**valid, "SECURE_COOKIES": "0"})
    refused({**valid, "DEV_AUTH_HEADERS": "1"})

    configured = Settings.from_env(valid)
    assert configured.secure_cookies is True
    assert configured.enable_docs is False
    assert configured.allow_dataset_seed is False
    assert configured.dev_auth_headers is False
    assert configured.dataset_max_age_hours == 36
    assert configured.auth_mode == "oidc"

    local = Settings.from_env({"APP_ENV": "local", "SESSION_SECRET": "l" * 40})
    assert local.auth_mode == "dev"
    assert local.enable_docs is True
    assert local.secure_cookies is False


def test_engine_subject_dual_control_requires_an_approval() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    approval = {
        "approval_id": "DC-test0001",
        "run_id": "run_test",
        "requested_by": "P. Raghavan",
        "requested_subject": "dev:controller",
        "approved_by": "S. Iyer",
        "approved_subject": "dev:admin",
        "reason": "Independent controller approval for a sole preparer.",
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
    }

    def sign_with(**overrides):
        return audit.sign(
            {"id": "run_test"}, RECON, STRICT, VERIFIED,
            signer="P. Raghavan", role="signer", note="",
            attestations=_attestations(VERIFIED), prev_chain_hash="genesis", sequence=1,
            preparer="P. Raghavan",
            signer_subject="dev:controller", preparer_subject="dev:controller",
            dual_control=True,
            **overrides,
        )

    record = sign_with(dual_control_approval=approval)
    assert record["dual_control"] is True
    assert record["dual_control_approval"]["approval_id"] == "DC-test0001"
    assert record["signer_subject"] == record["preparer_subject"] == "dev:controller"
    assert record["chain_hash"] == audit.digest(audit._chain_payload(record))

    try:
        sign_with()
    except audit.SignOffRefused as exc:
        assert "approved exception" in str(exc)
    else:
        raise AssertionError("subject-based dual control without an approval was accepted")

    expired = {**approval, "expires_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds")}
    try:
        sign_with(dual_control_approval=expired)
    except audit.SignOffRefused as exc:
        assert "expired" in str(exc)
    else:
        raise AssertionError("an expired dual-control approval was accepted")

    conflict = {**approval, "approved_subject": "dev:controller"}
    try:
        sign_with(dual_control_approval=conflict)
    except audit.SignOffRefused as exc:
        assert "different from the preparer" in str(exc)
    else:
        raise AssertionError("the preparer approved their own dual-control exception")

    wrong_run = {**approval, "run_id": "run_other"}
    try:
        sign_with(dual_control_approval=wrong_run)
    except audit.SignOffRefused as exc:
        assert "different run" in str(exc)
    else:
        raise AssertionError("an approval for another run was accepted")


# --------------------------------------------------------------- runner
def main() -> int:
    started = time.time()
    setup()
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
        except Exception:
            failures += 1
            print(f"FAIL  {name}  ({time.time() - t0:.2f}s)")
            traceback.print_exc()
        else:
            print(f"ok    {name}  ({time.time() - t0:.2f}s)")
    elapsed = time.time() - started
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed in {elapsed:.2f}s")
    if failures:
        print(f"database: {os.environ['FINCOM_DB']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
