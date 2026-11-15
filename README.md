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
at closing rates, computes plan variances, runs 151 controls (trial-balance
proof, roll-forward, subledger tie-out, intercompany elimination, FX
completeness, period completeness) and emits **862 facts**. Each fact carries
its own derivation: which journal lines, which plan row, which FX rate, and
how it was signed for presentation.

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

`app/engine/verify.py` trusts nothing the narrator produced. It runs five
independent passes:

* **Prose → fact (L1):** parse each rendered figure back off the page, check it
  cites a fact, and check the displayed value rounds from that fact at the
  stated scale and precision. Text is scanned against a closed quantity
  vocabulary, so a quantity written in words ("forty thousand dollars", "a
  fifth of revenue"), an approximate number ("roughly 40") or a vague
  quantifier ("hundreds of") is treated as an unbacked figure and blocks the
  section. Years, quarter labels, hyphenated compounds ("one-off") and
  unitless spelled counts remain prose.
* **Claim → section (L2):** every cited fact must belong to the section it
  appears in. Direction and comparator language is parsed as a grammar —
  subject, verb, direction phrase and comparator phrase — rather than matched
  against a word list. "Above plan", "in line with plan" and "above the revised
  plan" are distinguished, and the comparator named in prose must be the one
  the section covers. Favorability language must agree with the account's
  policy.
* **Fact → source (L3):** re-derive all 862 facts from the raw journal lines,
  plan rows and FX table, including presentation sign and Balance Sheet
  roll-forward. A stored fact is never taken at face value.
* **Section → document (L4):** the sections are reconciled with each other.
  For every driver id, the figures cited across sections must equal the
  driver's total in the reconciled attribution behind the narrated variances,
  and no driver figure may be told twice. Prose that calls a driver the
  primary cause, says the attribution is complete, or says the entire movement
  sits in one bucket is checked against that arithmetic. The summary may only
  claim the group gap is fully explained if the sections' operating-income
  effect adds up to it.
* **Narrative quality:** materiality coverage (12/12), driver disclosure,
  attribution closure (12/12), and unsourced-figure detection.

On every verification run the verifier also attacks itself with fourteen mutations:
`scale_error`, `sign_flip`, `rounding_drift`, `unsourced_figure`,
`spelled_out_amount`, `wrong_scope`, `basis_swap`, `summary_section_scale`,
`two_figure_sign_and_scale`, `wrong_comparator_and_scope`,
`prose_comparator_swap`, `driver_double_count`, `primary_driver_overstated`,
`summary_coverage_overstatement`. They cover the summary section and compound
edits — a sign flip plus a scale error in one paragraph, a wrong comparator
plus a wrong entity in one section, prose that names a revised plan while the
section is scoped to budget, a quantity written in words so a digit-only scan
would miss it, a driver figure told in two sections, a smaller cause named as
primary, and a summary that claims more coverage than the sections deliver. The
run reports how many it caught and how many figures each mutation broke
(currently 14/14), so the gate is demonstrated rather than asserted.

### 4. Sign off

`app/engine/audit.py` refuses a signature unless every gate condition passes
and every warning has been individually attested with a rationale. Each
warning category has its own attestation template, so a waiver answers the
question an auditor asks next: a control break must name the control and give a
remediation owner and date, and a disclosure gap must name who reviewed it and
when the narrative will be completed. A prose-only attestation is refused.

Every signed record also stamps the **engine version** and **verifier version**
that produced and cleared the commentary, and the certificate shows both.
Without that, a later change to either stage would be invisible in the audit
trail and a re-run could not be told apart from a re-interpretation of the same
evidence.

**Separation of duties.** The run's `run.created` event is the system of
record for who prepared it: the event actor is the preparer of record and its
detail carries the preparer's immutable subject and issuer. Identity is derived
from the authenticated session, never from the sign-off request. A signature is
refused when the signer's subject matches the preparer's, unless an
administrator has approved a time-boxed dual-control exception for that run. The
preparer cannot approve their own exception, and the approval's id, approver
subject, reason and expiry are committed into the signed record.

The record contains:

* hashes of the content, the verification report and the fact store,
* inside the fact-store hash, each fact's **full derivation** — journal line
  ids and amounts, FX rate, opening period, driver legs, aggregate accounts and
  source reference — so repointing a fact at different evidence cannot leave
  the certificate intact,
* the signer and preparer subjects and issuers, so identity is provable rather
  than typed, plus any dual-control approval that waived separation of duties,
* the attestations and the signer's note,
* a monotonic sequence number (`SO-0001`, `SO-0002`, …) that orders the chain
  independently of the second-resolution wall-clock timestamp,
* the previous record's chain hash,
* a chain hash over all of the above, including the sequence.

The audit trail re-hashes the live artefacts when it verifies the chain, so an
edit to signed commentary, verification or facts is detected as a broken link —
not just a rewritten hash field.

### 5. Compare runs

`app/engine/diff.py` compares two persisted runs of the same period. It indexes
the two fact stores by semantic identity (kind, entity, account, period, basis,
comparator and driver) rather than by fact id, and reports which figures moved
with both values and the delta, which variances changed actual, budget,
variance or materiality, which findings were added, cleared or reworded, and
which commentary sections changed wording or figure citations. It then decides
whether an existing signature still covers the comparison run: a signed base run
is invalidated when the artefact hashes differ or its chain link no longer
verifies. The comparison is a read model; it never mutates a run or a signature.

### 6. Roll forward

`app/engine/rollforward.py` reconciles every configured period through the same
engine and pivots entity × account × period, July through October with the
current period on the right. Entity and statement filters apply, materiality is
based on each cell, and `GRP` rows aggregate actual, budget, variance and prior
variance and recompute materiality at group level — a movement can sit below
every entity threshold and still clear the group threshold.

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

APP_ENV=local AUTH_MODE=dev \
SESSION_SECRET=local-development-secret-at-least-32-chars \
ALLOWED_HOSTS=127.0.0.1,localhost \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

The SQLite database is created at `data/close.db` on first request. The
synthetic dataset (`data/dataset.json`) is generated deterministically on first
use if missing.

Local development uses `AUTH_MODE=dev`: the sign-in screen offers seeded
identities (preparer, signer, reviewer, auditor, controller, admin), and the
top-bar switcher changes identity without restarting. `APP_ENV=local` is the
default. `run.sh` relies on those defaults; setting `SESSION_SECRET` keeps
sessions across restarts.

### Tests

```bash
.venv/bin/python -m tests.test_ledgerline
# 50/50 tests passed
```

50 tests cover the money round-trip, the quantity vocabulary, cross-section
driver and prose-claim reconciliation, reconcile invariants, the blocking
trial-balance proof, sequence-ordered chain verification, period-correct
narration, separation of duties, semantic direction and comparator parsing,
fact-bound narration, the clean and adversarial verification outcomes, the
fourteen-mutation self-test (including summary, compound,
spelled-out-quantity and cross-section edits), sign-off refusal, structured
attestation templates, chain tamper detection, stable evidence-pack ids and
per-download hashes, engine and verifier version stamps, run comparison and
signature invalidation, the multi-period roll-forward pivot, and the security
perimeter: authentication, per-role authorization, CSRF, host/body/rate limits,
request-id error envelopes, readiness, fail-closed production settings,
admin-approved dual control and legacy-record compatibility. No pytest
dependency. The UI is checked with `node --check app/static/app.js`.

## Tour

0. **Sign in** — local development offers seeded identities; production uses
   corporate SSO and derives roles from IdP groups. The top bar shows the
   authenticated person and role, and a dev identity switcher.
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
5. **Sign-off & audit** — gate conditions, per-warning attestation with a
   category template (control reference, remediation owner and date for a
   control break), the authenticated signer's identity card, and the
   hash-chained audit trail. After signing, the certificate shows all artefact
   hashes, signer/preparer subjects, dual-control approval (when used), engine
   and verifier versions and attested exceptions.
6. **Run comparison** — pick two persisted runs and see which figures moved,
   which findings were added or cleared, which sections changed and whether the
   base signature still covers the comparison run.
7. **Roll-forward** — switch the workbench between the current period and a
   July–October period × account pivot, with entity and statement filters and
   group rows whose materiality is recomputed at group level.

Export the evidence pack (JSON, ~1.4 MB) or the review sheet (CSV, one row per
section) from the top bar or the sign-off certificate. A pack carries a stable
`pack_id` — a hash of the signed artefacts, so two downloads of the same run
can be shown to describe the same evidence — plus a `pack_hash` over each
download so the bytes can be verified after transfer.

## HTTP API

Every financial route requires an authenticated session and a role permission.
State-changing requests also require the `X-CSRF-Token` header. Errors use one
envelope, `{"error": {"code", "message", "request_id"}}`, and every response
carries `X-Request-ID`.

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/health` | public | Liveness only |
| `GET` | `/api/ready` | public | Database, dataset freshness/contract and chain integrity (`503` when unhealthy) |
| `GET` | `/api/auth/me` | public | Session state, principal, permissions, CSRF token, dev identities |
| `POST` | `/auth/dev/login` | dev only | Start a seeded local identity session |
| `GET` | `/auth/login` | public | Corporate OIDC redirect (authorization code + PKCE) |
| `GET` | `/auth/callback` | public | OIDC callback; creates the BFF session |
| `POST` | `/auth/logout` | session | Revoke the session and clear cookies |
| `GET` | `/api/meta` | `meta:read` | Group, entities, accounts, policy, source systems |
| `GET` | `/api/runs` | `run:read` | Recent runs |
| `POST` | `/api/runs` | `run:create` | Reconcile, narrate and verify a new run (`period`, `mode`) |
| `GET` | `/api/runs/{id}` | `run:read` | Full workpaper view including viewer capabilities |
| `GET` | `/api/runs/{id}/diff/{other_id}` | `run:read` | Compare two runs (`base` vs `compare`) |
| `GET` | `/api/rollforward` | `run:read` | Period × account pivot (`entity`, `statement`, `material_only`) |
| `POST` | `/api/runs/{id}/renarrate` | `run:narrate` | Re-draft (`strict` or `drift`) and re-verify |
| `POST` | `/api/runs/{id}/verify` | `run:verify` | Re-run verification on the current draft |
| `GET` | `/api/runs/{id}/facts/{fact}` | `fact:read` | One fact plus its verification check |
| `POST` | `/api/runs/{id}/signoff` | `run:sign` | Sign with `note` and per-warning `attestations`; identity is server-derived |
| `GET` | `/api/runs/{id}/dual-control` | `run:read` | Active approval and its history |
| `POST` | `/api/runs/{id}/dual-control` | `policy:manage` | Admin approves a time-boxed exception (`reason`, optional `expires_at`) |
| `DELETE` | `/api/runs/{id}/dual-control` | `policy:manage` | Revoke the active approval |
| `GET` | `/api/runs/{id}/export` | `export:read` | Evidence pack (JSON) |
| `GET` | `/api/runs/{id}/export.csv` | `export:read` | Commentary review sheet (CSV) |
| `GET` | `/api/audit/chain` | `chain:read` | Re-verified hash chain |

Signed runs are frozen: re-narration, re-verification, a second signature and a
new dual-control approval are all refused with `409`.

Configuration is environment-driven and fail-closed in staging and production.
See [`docs/production/phase-1/`](docs/production/phase-1/README.md) for the full
variable list, role matrix and threat coverage.

## Layout

```
app/
  config.py            entities, accounts, FX, materiality, driver taxonomy
  money.py             Decimal rendering/parsing shared by narrator and verifier
  seed.py              deterministic synthetic close
  settings.py          environment settings and fail-closed production rules
  security.py          middleware, request ids, limits, headers, error envelope
  auth.py              Principal, roles, sessions, CSRF, OIDC claim mapping
  auth_routes.py       OIDC/dev login, callback, logout, session state
  store.py             SQLite runs, events, sign-offs, sessions and approvals
  main.py              FastAPI surface with per-route authorization
  engine/
    reconcile.py       trial balance, variances, drivers, controls, facts
    narrate.py         fact-constrained claim plan (+ deliberate drift mode)
    verify.py          five verification layers + fourteen-mutation self-test
    audit.py           hashes, preflight, signature, chain, exports
    diff.py            run comparison and signature invalidation
    rollforward.py     multi-period pivot and period-scoped datasets
  static/
    index.html         single page
    styles.css         workpaper design system
    app.js             all UI logic, no build step
tests/test_ledgerline.py
```

## Demo dataset

Acme Global Holdings, four entities (US01 USD, DE02 EUR, SG03 SGD, UK04 GBP),
four periods (2026-07 through 2026-10), 20 accounts, 1,072 journal lines, 76
account variances (12 material: 11 P&L, 1 balance sheet), 151 controls with
three deliberate warn-level breaks (SG03 cash subledger, IC-01 and IC-02
intercompany timing/FX differences), and 862 facts. The ledger is double-entry
and every entity trials balance; `TB-{entity}` and `TB-GRP` block rather than
warn, so an unbalanced ledger can never reach sign-off.

Everything is synthetic and seeded deterministically; no customer data is
included.

## Scope and limits

This is a complete, runnable product demo, not a multi-tenant deployment. The
security perimeter is real: identity is derived from an OIDC or local-dev
session, every financial route is authorized, and sign-off is bound to the
authenticated subject. What remains a demo is the system of record: SQLite, an
in-process sign-off lock and an unkeyed hash chain. The narration template is
deterministic rather than an external model call. The verification and audit
layers are real: the numbers, hashes and refusals are computed, not mocked.

## Production readiness

The Phase 0 decision pack — deployment model, identity, data store, runtime,
data-flow and threat model — is in
[`docs/production/phase-0/`](docs/production/phase-0/README.md). The four
architecture decisions and Phase 1 scope were accepted by the decision owner on
2026-11-15.

Phase 1 — the security perimeter — is implemented and verified in the
local/test profile: see
[`docs/production/phase-1/`](docs/production/phase-1/README.md) for the control
matrix, threat coverage, environment variables, test evidence and residual
risks.

Production data remains blocked until Phase 2 delivers PostgreSQL with
migrations and atomic sign-off, append-only audit tables, KMS-backed
signatures, WORM evidence packs, encrypted backups with a tested restore, and a
live corporate OIDC integration test.
