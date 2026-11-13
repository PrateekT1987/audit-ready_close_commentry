# Ledgerline — Audit-Ready Close Commentary

Financial close commentary is still written by hand: an analyst opens the
ledger, the plan, the FX table and last month's file, then types numbers into a
memo. Every typed number is a chance to transcribe the wrong cell, drift a
scale, or state a cause the numbers do not support. By the time the CFO signs,
nobody can prove which sentence came from which source row.

Ledgerline makes the commentary a **claim plan**, not prose. Every figure in
the memo is a reference to a reconciled fact. The agent can choose *which*
fact to cite and *how* to describe it, but it cannot type a number. Before a
human signs, the system parses every figure back off the rendered page,
re-derives every fact from the journal extract, and re-checks the direction and
favorability of every claim.

```
reconcile  →  narrate  →  verify  →  human sign-off
```

The sign-off binds the commentary, the verification report and the source fact
store into one hash-chained audit record.

## Why this is different from "an LLM writes the variance commentary"

| Typical approach | Ledgerline |
| --- | --- |
| Model writes text containing numbers | Model writes a fact plan; the renderer inserts numbers |
| Reviewer spot-checks a few figures | Every figure is parsed back and compared to its fact |
| Source values are trusted as stored | Every fact is independently re-derived from journal lines, plan and FX |
| "Looks reasonable" is the gate | Coverage, scope, basis, direction, favorability and closure are enforced |
| Sign-off is a button | Sign-off is a hashed record, chained to the previous one |

## The pipeline

### 1. Reconcile

`app/engine/reconcile.py` builds the trial balance, translates local balances
at closing rates, computes plan variances, runs 146 controls (roll-forward,
subledger tie-out, intercompany elimination, FX completeness, period
completeness) and emits **862 facts**. Each fact carries its own derivation:
which journal lines, which plan row, which FX rate, and how it was signed for
presentation.

Variances are decomposed into named drivers. The attribution must sum exactly
to the variance; a residual is a control failure, not a rounding note.

### 2. Narrate

`app/engine/narrate.py` drafts 13 sections from a deterministic template:
a group summary plus one section per material variance. Each section has three
paragraph roles — movement, attribution, quality — and every number is a
`fig` token (`{t: "fig", fact: "F-0001", text, number, scale, decimals, unit,
sign_mode}`). There is no code path that writes a numeral into prose.

`mode="drift"` deliberately injects two realistic model failures into the
largest variance section:

* an unsourced `$40K` figure in prose (a hallucinated number), and
* the prior-period variance cited where the budget variance belongs
  (a wrong-cell reference).

This exists to prove the gate blocks; it is not a feature of normal operation.

### 3. Verify

`app/engine/verify.py` trusts nothing the narrator produced. It runs four
independent layers:

* **Prose → fact (L1):** parse each rendered figure back off the page, check it
  cites a fact, and check the displayed value rounds from that fact at the
  stated scale and precision.
* **Claim → section (L2):** every cited fact must belong to the section it
  appears in, and the direction and favorability language must agree with the
  fact's sign and the account's policy.
* **Fact → source (L3):** re-derive all 862 facts from the raw journal lines,
  plan rows and FX table, including presentation sign and Balance Sheet
  roll-forward. A stored fact is never taken at face value.
* **Narrative quality:** materiality coverage (12/12), driver disclosure,
  attribution closure (12/12), and unsourced-figure detection.

On every verification run the verifier also attacks itself with six mutations:
`scale_error`, `sign_flip`, `rounding_drift`, `unsourced_figure`,
`wrong_scope`, `basis_swap`. The run reports how many it caught (currently 6/6),
so the gate is demonstrated rather than asserted.

### 4. Sign off

`app/engine/audit.py` refuses a signature unless every gate condition passes
and every warning has been individually attested with a rationale. The record
contains:

* hashes of the content, the verification report and the fact store,
* the attestations and the signer's note,
* the previous record's chain hash,
* a chain hash over all of the above.

The audit trail re-hashes the live artefacts when it verifies the chain, so an
edit to signed commentary, verification or facts is detected as a broken link —
not just a rewritten hash field.

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
./run.sh
# Ledgerline is at http://127.0.0.1:8765
```

Or manually:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

The SQLite database is created at `data/close.db` on first request. The
synthetic dataset (`data/dataset.json`) is generated deterministically on first
use if missing.

### Tests

```bash
.venv/bin/python -m tests.test_ledgerline
```

16 tests cover the money round-trip, reconcile invariants, fact-bound
narration, the clean and adversarial verification outcomes, the six-mutation
self-test, sign-off refusal, chain tamper detection, evidence-pack manifests
and the HTTP surface. No pytest dependency.

## Tour

1. **Overview** — group P&L vs plan, KPI strip, control results, entity cards,
   and a plain-language account of how the close was produced.
2. **Variance workbench** — 76 account variances, filterable by entity,
   statement and materiality. Expand a row to see its driver attribution and
   the exact source facts for actual, budget and variance.
3. **Commentary** — the memo as rendered. Click any figure to open the source
   drawer: stored value, recomputed value, derivation record and method.
   *Run adversarial test* injects the two drift defects and takes you to the
   blocked verification.
4. **Verification** — gate checklist, KPI strip, verifier self-test, findings
   (block/warn/info) and the full claim ledger: every figure as written next to
   its source value and pass/fail status.
5. **Sign-off & audit** — gate conditions, per-warning attestation with
   rationale, the signature form, and the hash-chained audit trail. After
   signing, the certificate shows all artefact hashes and attested exceptions.

Export the evidence pack (JSON, ~1.4 MB) or the review sheet (CSV, one row per
section) from the top bar or the sign-off certificate.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness |
| `GET` | `/api/meta` | Group, entities, accounts, policy, source systems |
| `GET` | `/api/runs` | Recent runs |
| `POST` | `/api/runs` | Reconcile, narrate and verify a new run |
| `GET` | `/api/runs/{id}` | Full workpaper view |
| `POST` | `/api/runs/{id}/renarrate` | Re-draft (`strict` or `drift`) and re-verify |
| `POST` | `/api/runs/{id}/verify` | Re-run verification on the current draft |
| `GET` | `/api/runs/{id}/facts/{fact}` | One fact plus its verification check |
| `POST` | `/api/runs/{id}/signoff` | Sign, with per-warning attestations |
| `GET` | `/api/runs/{id}/export` | Evidence pack (JSON) |
| `GET` | `/api/runs/{id}/export.csv` | Commentary review sheet (CSV) |
| `GET` | `/api/audit/chain` | Re-verified hash chain |

Signed runs are frozen: re-narration, re-verification and a second signature
are all refused with `409`.

## Layout

```
app/
  config.py            entities, accounts, FX, materiality, driver taxonomy
  money.py             Decimal rendering/parsing shared by narrator and verifier
  seed.py              deterministic synthetic close
  store.py             SQLite runs, events and sign-off records
  main.py              FastAPI surface
  engine/
    reconcile.py       trial balance, variances, drivers, controls, facts
    narrate.py         fact-constrained claim plan (+ deliberate drift mode)
    verify.py          four verification layers + six-mutation self-test
    audit.py           hashes, preflight, signature, chain, exports
  static/
    index.html         single page
    styles.css         workpaper design system
    app.js             all UI logic, no build step
tests/test_ledgerline.py
```

## Demo dataset

Acme Global Holdings, four entities (US01 USD, DE02 EUR, SG03 SGD, UK04 GBP),
four periods (2026-07 through 2026-10), 20 accounts, 1,065 journal lines, 76
account variances (12 material: 11 P&L, 1 balance sheet), 146 controls with
three deliberate warn-level breaks (SG03 cash subledger, IC-01 and IC-02
intercompany timing/FX differences), and 862 facts.

Everything is synthetic and seeded deterministically; no customer data is
included.

## Scope and limits

This is a complete, runnable product demo, not a multi-tenant deployment. There
is no authentication, the SQLite file is local, and the narration template is
deterministic rather than an external model call. The verification and audit
layers are real: the numbers, hashes and refusals are computed, not mocked.
