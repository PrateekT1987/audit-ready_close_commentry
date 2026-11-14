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
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    base = client.post("/api/runs", json={"mode": "strict", "prepared_by": "J. Okafor"}).json()
    base_id = base["run"]["id"]
    signed = client.post(f"/api/runs/{base_id}/signoff", json={
        "signer": "A. Mensah", "attestations": _attestations(base["verification"]),
    })
    assert signed.status_code == 201, signed.text

    # A clean re-run of the same period: identical artefacts, signature intact.
    same = client.post("/api/runs", json={"mode": "strict", "prepared_by": "J. Okafor"}).json()
    same_diff = client.get(f"/api/runs/{base_id}/diff/{same['run']['id']}").json()
    assert same_diff["same_period"] is True
    assert same_diff["summary"]["facts_changed"] == 0
    assert same_diff["summary"]["findings_added"] == 0
    assert same_diff["summary"]["sections_changed"] == 0
    assert same_diff["hashes"]["match"] is True
    assert same_diff["signoffs"]["invalidated"] is False
    assert same_diff["versions"]["engine_changed"] is False
    assert same_diff["versions"]["verifier_changed"] is False

    # An adversarial re-narration changes the figures and raises findings.
    drift = client.post("/api/runs", json={"mode": "drift", "prepared_by": "J. Okafor"}).json()
    diff = client.get(f"/api/runs/{base_id}/diff/{drift['run']['id']}").json()
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
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
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
def test_http_pipeline() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    assert client.get("/api/health").json()["ok"] is True
    meta = client.get("/api/meta").json()
    assert meta["group"] == "Acme Global Holdings"

    created = client.post("/api/runs", json={"mode": "strict"})
    assert created.status_code == 201, created.text
    view = created.json()
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "attestation"
    assert view["verification"]["gate"]["blocking"] == 0
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert client.get("/api/runs/run_missing").status_code == 404

    fact_id = view["recon"]["facts"][0]["fact_id"]
    fact = client.get(f"/api/runs/{run_id}/facts/{fact_id}").json()
    assert fact["check"]["status"] == "pass"

    # Signature refused without attestations, accepted with them.
    warn_ids = [f["finding_id"] for f in view["verification"]["findings"] if f["severity"] == "warn"]
    refused = client.post(f"/api/runs/{run_id}/signoff", json={
        "signer": "A. Mensah", "role": "Group Controller", "attestations": [],
    })
    assert refused.status_code == 409
    assert "attested" in refused.json()["error"]

    signed = client.post(f"/api/runs/{run_id}/signoff", json={
        "signer": "A. Mensah",
        "role": "Group Controller",
        "note": "Reviewed with the CFO.",
        "attestations": _attestations(view["verification"]),
    })
    assert signed.status_code == 201, signed.text
    signed_view = signed.json()
    assert signed_view["run"]["status"] == "signed"
    assert signed_view["signoff"]["counts"]["warnings_attested"] == len(warn_ids)
    assert signed_view["chain"][-1]["status"] == "intact"

    # A signed record is frozen.
    assert client.post(f"/api/runs/{run_id}/renarrate", json={"mode": "strict"}).status_code == 409
    assert client.post(f"/api/runs/{run_id}/verify").status_code == 409
    assert client.post(f"/api/runs/{run_id}/signoff", json={
        "signer": "Someone Else", "attestations": [],
    }).status_code == 409

    exported = client.get(f"/api/runs/{run_id}/export")
    assert exported.status_code == 200
    pack = exported.json()
    assert pack["manifest"]["content_hash"] == signed_view["signoff"]["hashes"]["content_hash"]
    assert pack["manifest"]["verification_hash"] == signed_view["signoff"]["hashes"]["verification_hash"]
    assert pack["manifest"]["fact_store_hash"] == signed_view["signoff"]["hashes"]["fact_store_hash"]
    assert pack["manifest"]["signoff_id"] == signed_view["signoff"]["signoff_id"]
    assert pack["pack_id"] == pack["manifest"]["pack_id"]
    assert audit.verify_pack_hash(pack)
    assert pack["chain"][-1]["status"] == "intact"

    # The stable id survives a second download; the per-download digest still verifies.
    again = client.get(f"/api/runs/{run_id}/export").json()
    assert again["pack_id"] == pack["pack_id"]
    assert audit.verify_pack_hash(again)

    csv = client.get(f"/api/runs/{run_id}/export.csv")
    assert csv.status_code == 200
    assert csv.text.splitlines()[0].startswith("section_id,variance_id")
    assert len(csv.text.splitlines()) == 14  # header + 13 sections

    chain = client.get("/api/audit/chain").json()
    assert chain["intact"] is True
    assert chain["signoffs"][-1]["signoff_id"] == signed_view["signoff"]["signoff_id"]


def test_http_separation_of_duties() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    created = client.post("/api/runs", json={
        "mode": "strict", "prepared_by": "P. Raghavan", "dual_control": False,
    })
    assert created.status_code == 201, created.text
    view = created.json()
    run_id = view["run"]["id"]
    assert view["run"]["preparer"] == "P. Raghavan"
    assert view["run"]["dual_control"] is False
    assert view["verification"]["gate"]["blocking"] == 0
    warn_ids = [f["finding_id"] for f in view["verification"]["findings"] if f["severity"] == "warn"]
    attestations = _attestations(view["verification"])

    refused = client.post(f"/api/runs/{run_id}/signoff", json={
        "signer": "p. raghavan", "attestations": attestations,
    })
    assert refused.status_code == 409
    assert "Separation of duties" in refused.json()["error"]

    signed = client.post(f"/api/runs/{run_id}/signoff", json={
        "signer": "A. Mensah", "attestations": attestations,
    })
    assert signed.status_code == 201, signed.text
    signoff = signed.json()["signoff"]
    assert signoff["signer"] == "A. Mensah"
    assert signoff["preparer"] == "P. Raghavan"
    assert signoff["dual_control"] is False
    assert signed.json()["chain"][-1]["status"] == "intact"

    # A dual-control run lets the preparer sign, and records the waiver.
    dual = client.post("/api/runs", json={
        "mode": "strict", "prepared_by": "P. Raghavan", "dual_control": True,
    }).json()
    dual_id = dual["run"]["id"]
    dual_attest = _attestations(dual["verification"])
    dual_signed = client.post(f"/api/runs/{dual_id}/signoff", json={
        "signer": "P. Raghavan", "attestations": dual_attest,
    })
    assert dual_signed.status_code == 201, dual_signed.text
    dual_record = dual_signed.json()["signoff"]
    assert dual_record["signer"] == dual_record["preparer"] == "P. Raghavan"
    assert dual_record["dual_control"] is True

    # A blank preparer is rejected when the run is opened.
    assert client.post("/api/runs", json={"mode": "strict", "prepared_by": "  "}).status_code == 422


def test_http_adversarial_then_recover() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    run_id = client.post("/api/runs", json={"mode": "strict"}).json()["run"]["id"]

    drift = client.post(f"/api/runs/{run_id}/renarrate", json={"mode": "drift"})
    assert drift.status_code == 200
    drift_view = drift.json()
    assert drift_view["run"]["status"] == "blocked"
    assert drift_view["verification"]["gate"]["blocking"] == 3
    assert client.post(f"/api/runs/{run_id}/verify").json()["run"]["status"] == "blocked"

    recovered = client.post(f"/api/runs/{run_id}/renarrate", json={"mode": "strict"}).json()
    assert recovered["run"]["status"] == "attestation"
    assert recovered["verification"]["gate"]["blocking"] == 0
    assert recovered["commentary"]["drift"]["applied"] is False


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
