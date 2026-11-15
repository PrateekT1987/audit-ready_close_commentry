# Phase 1 — Security perimeter

**Status:** Implemented and verified in the local/test profile — 2026-11-15
**Owner:** Engineering (security), with Security and Finance review at Phase 1
kickoff
**Applies to:** Ledgerline API and UI (`app/`)
**Depends on:** [Phase 0 decision pack](../phase-0/README.md) (ADR-001 to ADR-004)

Phase 1 removes the demo's self-declared identity and unprotected HTTP surface.
It does **not** make the application production-ready on its own: the system of
record is still SQLite, signatures are still an unkeyed hash chain, and evidence
packs are still written to the local filesystem. Those remain Phase 2 work
(ADR-003 and ADR-004), and production data stays blocked until they land.

## What changed

| Area | Before | After |
| --- | --- | --- |
| Identity | `prepared_by` and `signer` were free-text request fields | Server-derived `Principal` from an OIDC session; identity fields removed from request schemas (`extra="forbid"`) |
| Authorization | Every route public | Per-route permissions; preparer, reviewer, signer, auditor and admin roles |
| Sessions | None | Server-side session records, opaque HttpOnly cookie, idle and absolute expiry, logout revocation |
| CSRF | None | Double-submit token bound to the server-side session plus `Origin` check on every state-changing request |
| Separation of duties | Caller-set `dual_control` boolean | Subject comparison; a self-signature needs an admin-approved, time-boxed exception committed into the signed record |
| Configuration | Hardcoded defaults | Environment-backed settings; staging and production fail closed at startup |
| HTTP hardening | GZip only | Host allowlist, security headers and CSP, CORS allowlist, body-size limit, per-bucket rate limits, consistent error envelope with request ids |
| Operations | `/api/health` only | `/api/ready` checks database, dataset presence/freshness/contract and audit-chain integrity; docs disabled outside local/test |
| Audit reads | Unlogged | Exports and all identity decisions record `actor_subject` and `session_id` in append-only events |

## Identity and session model

```
browser ──(authorization code + PKCE)──▶ corporate IdP (MFA, conditional access)
   │                                            │
   │◀──────────── opaque session cookie ────────┘
   ▼
Ledgerline BFF ── server-side session row ──▶ Principal(subject, issuer, roles)
   │
   └── every audit record reads identity from the Principal, never the body
```

- `AUTH_MODE=oidc` registers the Authlib client from `OIDC_ISSUER`,
  `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `OIDC_REDIRECT_URI`, using the
  issuer's discovery document with S256 PKCE.
- The browser never receives an access or ID token. The session cookie is
  HttpOnly, `SameSite=Lax`, `Secure` in protected environments, and carries a
  random token whose SHA-256 is the database key.
- Roles are mapped server-side from `OIDC_GROUPS_CLAIM` through `OIDC_ROLE_MAP`.
  Unmapped groups grant nothing; `OIDC_DEFAULT_ROLES` is an explicit, empty by
  default, fallback.
- Session rows carry `idle_expires_at` and `expires_at`; both are checked on
  every request. Logout revokes the row immediately.
- `AUTH_MODE=dev` is local/test only. It offers seeded identities in the UI and,
  when `DEV_AUTH_HEADERS=1`, `X-Dev-Subject` / `X-Dev-Roles` headers for local
  machine testing. Both settings are refused at startup in staging and
  production.

## Authorization model

| Role | Create run | Re-narrate / verify | Sign | Read facts / chain | Export | Manage policy |
| --- | --- | --- | --- | --- | --- | --- |
| preparer | yes | yes | no | yes | yes | no |
| reviewer | no | no | no | yes | no | no |
| signer | no | no | yes | yes | yes | no |
| auditor | no | no | no | yes | yes | no |
| admin | yes | yes | no (unless also signer) | yes | yes | yes |

`admin` is a policy wildcard, not a way around separation of duties: the
approver of a dual-control exception must be a different subject from the
preparer, and the signer must still satisfy the gate and attest every warning.

## Separation of duties and dual control

1. The `run.created` event records `preparer_subject` and `preparer_issuer`
   from the authenticated session. The signer reads governance from that
   append-only event, not from the run payload.
2. Sign-off derives `signer`, `signer_subject`, `signer_issuer`,
   `preparer_subject` and `preparer_issuer` server-side. Request fields for any
   of these are rejected with `422`.
3. If signer subject equals preparer subject, sign-off returns
   `409 separation_of_duties` unless an active admin approval exists.
4. An approval is created by an account with `policy:manage`, must name a reason
   (10–1000 characters), expires within seven days (default 24 hours), and is
   itself refused when the approver prepared the run. Revocation is immediate.
5. A used approval is embedded in the signed record as
   `dual_control_approval` and committed by `chain_hash`, including the
   approver subject, reason and expiry. The approval record and the sign-off
   event are both in the audit trail.

## Production fail-closed rules

`Settings.from_env` refuses to start when `APP_ENV` is `staging` or
`production` and any of the following is true:

- `AUTH_MODE` is not `oidc`;
- `SESSION_SECRET` is missing or shorter than 32 characters;
- `ALLOWED_HOSTS` was not set explicitly, is empty, or contains `*`;
- `PUBLIC_ORIGIN` is missing or not `https://`;
- `SECURE_COOKIES` is disabled;
- `DEV_AUTH_HEADERS` is enabled;
- OIDC is selected but issuer, client id, secret or redirect URI is missing, or
  issuer/redirect URI is not `https://`;
- `OIDC_ROLE_MAP` is not a JSON object or maps to an unknown role.

Protected defaults also disable API docs and synthetic dataset seeding, and set
dataset freshness to 36 hours.

## Environment variables

| Variable | Default (local/test) | Purpose |
| --- | --- | --- |
| `APP_ENV` | `local` | `local`, `test`, `dev`, `staging`, `production` |
| `AUTH_MODE` | `dev` (`oidc` in protected) | `dev` or `oidc` |
| `SESSION_SECRET` | ephemeral in local, required ≥32 chars in protected | Session and OIDC state signing |
| `SESSION_COOKIE_NAME` | `ledgerline_session` | Session cookie name |
| `SESSION_IDLE_MINUTES` | `480` | Idle timeout (minimum 5) |
| `SESSION_ABSOLUTE_HOURS` | `12` | Absolute session lifetime |
| `SECURE_COOKIES` | `false` (`true` in protected) | `Secure` attribute |
| `ALLOWED_HOSTS` | `127.0.0.1,localhost,testserver` | Host allowlist by hostname (no port); exact or glob |
| `CORS_ORIGINS` | empty | Explicit browser origins, if any |
| `PUBLIC_ORIGIN` | empty | Canonical browser origin for CSRF checks |
| `MAX_BODY_BYTES` | `1048576` | Request body ceiling |
| `ENABLE_DOCS` | `true` (`false` in protected) | OpenAPI, `/docs`, `/redoc` |
| `ALLOW_DATASET_SEED` | `true` (`false` in protected) | Synthetic seeding when the dataset is missing |
| `DATASET_MAX_AGE_HOURS` | `0` (`36` in protected) | Readiness freshness gate; `0` disables |
| `DEV_AUTH_HEADERS` | `true` (`false` in protected) | Local `X-Dev-*` identity headers |
| `RATE_LIMIT_READ_PER_MINUTE` | `600` | Read bucket |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `120` | Write bucket |
| `RATE_LIMIT_EXPORT_PER_MINUTE` | `30` | Evidence-pack and CSV bucket |
| `RATE_LIMIT_AUTH_PER_MINUTE` | `30` | Login bucket |
| `OIDC_ISSUER` | empty | Issuer base URL; discovery is appended |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | empty | Client credentials |
| `OIDC_REDIRECT_URI` | empty | Registered callback URL |
| `OIDC_SCOPES` | `openid profile email` | Requested scopes |
| `OIDC_GROUPS_CLAIM` | `groups` | Claim carrying group membership |
| `OIDC_ROLE_MAP` | `{}` | JSON object of `group -> role` |
| `OIDC_DEFAULT_ROLES` | empty | Fallback roles when no group maps |

## HTTP controls

- **Request id.** Every response carries `X-Request-ID`; a well-formed client
  value is preserved, otherwise one is generated. Errors return the same id in
  `error.request_id`.
- **Error envelope.** `{"error": {"code", "message", "request_id", "details"?}}`
  for deliberate API errors, validation errors, HTTP errors and unhandled
  exceptions. Unhandled exceptions log the stack server-side and return a
  generic message.
- **Host allowlist.** Checked before routing, for every path, including static
  assets. Refusal returns `400 invalid_host`.
- **Body limit.** Declared `Content-Length` over `MAX_BODY_BYTES` returns
  `413 payload_too_large`; schemas add field-level maximum lengths.
- **Rate limits.** In-process sliding windows per principal (or client IP before
  authentication) and per bucket, with `Retry-After`. This is a backstop; the
  ingress/WAF quota is authoritative behind multiple replicas.
- **Headers.** CSP (self-only scripts, no framing, no object embedding),
  `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`, COOP/CORP, HSTS when HTTPS or secure cookies, and
  `Cache-Control: no-store` for API and shell responses.
- **CSRF.** State-changing requests require `X-CSRF-Token` matching the
  server-side session hash and, when an `Origin` header is present, that origin
  must be in `PUBLIC_ORIGIN`, `CORS_ORIGINS` or the request base URL. Login and
  callback are exempt because they establish the session.
- **Docs.** `/docs`, `/redoc` and `/openapi.json` are not registered in
  staging or production.
- **Readiness.** `/api/ready` returns `503` unless the database responds, the
  dataset is present and fresh, its contract is valid, and every sign-off link
  verifies.

## Threat coverage

Ratings are from the Phase 0 threat model. "Residual" is the risk left after
Phase 1 and before Phase 2.

| Threat | Phase 1 control | Evidence | Residual |
| --- | --- | --- | --- |
| S-01 caller signs as any name | Server-derived signer subject; `signer` rejected in body | `test_http_pipeline`, `test_http_validation_bounds_and_enum_values` | Unkeyed hash still forgeable by a DB writer (Phase 2 KMS) |
| S-02 preparer claims another preparer | Preparer from session, stored as subject in `run.created` | `test_http_pipeline`, `test_http_separation_of_duties_and_admin_dual_control` | `events` table still mutable (Phase 2 append-only) |
| S-03 stolen session or replay | HttpOnly/Secure/SameSite cookie, hashed server-side token, idle and absolute expiry, revocation | `test_http_csrf_and_logout`, `test_production_settings_fail_closed` | No IdP back-channel logout or token binding yet |
| S-04 issuer/audience confusion | Issuer checked against configured value; roles deny by default | Code path in `principal_from_claims`; settings tests | No live IdP integration test yet (credentials pending) |
| S-05 admin impersonation | Named roles, `policy:manage` only for admin, approvals audited | `test_http_role_based_access_control` | No break-glass account process or SIEM alerting yet |
| E-01 preparer self-waives SoD | Admin approval record with conflict check, expiry, revocation, committed into signature | `test_http_separation_of_duties_and_admin_dual_control`, `test_engine_subject_dual_control_requires_an_approval` | Approval table still locally mutable (Phase 2) |
| E-02 role claim tampering | Server-side group→role map, unknown roles refused at startup | `test_production_settings_fail_closed` | Mapping reviewed only at deployment time |
| E-03 admin edits a signed run | No mutation route for signed runs; approvals refused after signature | `test_http_pipeline`, `test_http_separation_of_duties_and_admin_dual_control` | No immutable storage or WORM yet |
| A-03 CSRF on sign-off | Double-submit token bound to session, Origin check | `test_http_csrf_and_logout` | In-process only; no distributed token store needed for BFF |
| A-05 host/proxy misconfiguration | Host allowlist before routing; wildcard refused in protected | `test_http_requires_authentication_and_uses_one_error_envelope`, `test_production_settings_fail_closed` | Proxy header trust policy belongs to ingress (Phase 2) |
| A-06 stale data accepted | Readiness freshness and dataset contract | `test_readiness_reports_each_dependency` | Ingestion lineage and source control totals still Phase 2 |
| I-01 anonymous pack download | RBAC on every financial route; export permission | `test_http_requires_authentication_and_uses_one_error_envelope`, `test_http_role_based_access_control` | Object storage is still local disk (Phase 2 WORM) |
| I-02 token theft by XSS | No browser tokens; CSP; output escaping | `test_http_requires_authentication_and_uses_one_error_envelope` | Full XSS review of every `innerHTML` is ongoing |
| I-06 over-broad access | Least-privilege permission sets per role | `test_http_role_based_access_control` | Access review cadence is an operating control |
| D-02 huge body exhausts memory | Body-size limit and bounded Pydantic fields | `test_http_body_limit_and_rate_limit_are_enforced`, `test_http_validation_bounds_and_enum_values` | Streaming/queue limits at ingress still Phase 2 |
| D-01 anonymous run flood | Authentication plus write rate limit | `test_http_body_limit_and_rate_limit_are_enforced` | Per-principal quota and retention policy still Phase 2 |
| R-03 export denied later | Export events record actor subject, pack id/hash | `test_http_pipeline` | SIEM forwarding is Phase 2 |

T-01 to T-07 (audit-chain integrity), R-01/R-02/R-04 (non-repudiation) and
I-03/I-04/I-05 (log hygiene, storage and backups) are explicitly **not**
closed by Phase 1. They require the Phase 2 data store, KMS signing and storage
work; see "Residual Phase 2 work" below.

## Tests

```bash
.venv/bin/python -m tests.test_ledgerline
```

`50/50 tests passed` (about 8.4 s). Phase 1 additions cover:

- anonymous access refused with a single error envelope and request id;
- public liveness/readiness/auth-state routes;
- security headers, CSP, `no-store`, and host allowlist refusal;
- dev login, CSRF cookie/header binding, wrong-token refusal and logout;
- preparer/reviewer/signer/auditor/admin permission boundaries;
- separation of duties, admin approval, revocation and approver-preparer
  conflict;
- removal of `prepared_by`, `signer` and `dual_control` from request schemas;
- bounded note, attestation and detail fields;
- body-size and per-bucket rate limits;
- readiness reporting each dependency;
- production fail-closed settings, including docs, seeding, cookies, hosts and
  OIDC completeness;
- engine-level subject-based dual control with approval validation;
- legacy sign-offs without subjects or sequence verifying unchanged.

The UI is syntax-checked with `node --check app/static/app.js`; it has no build
step.

## Live smoke test

A local Uvicorn run on `127.0.0.1:8765` with a temporary database confirmed:

- anonymous `/api/runs` → `401`, `/api/health` → `200`;
- dev login → session plus CSRF cookie;
- missing CSRF → `403`, with the same token in the error envelope;
- `controller` creates a run; `preparer_subject=dev:controller`;
- self-sign → `409 separation_of_duties`;
- admin approval → `201`, approval bound to the run;
- self-sign with approval → `201`, `dual_control=true`, approval id in the
  signed record, chain intact;
- evidence export → `200` with `X-Pack-ID`, `X-Pack-Hash` and a fresh
  `X-Request-ID`.

A copy of the legacy pilot database was also re-verified: all five sign-offs
(`SO-0001`…`SO-0005`) remain intact, and all five correctly lack
`signer_subject`. Existing signed evidence is not invalidated by Phase 1.

## Residual Phase 2 work

Ordered by the production blockers they remove:

1. **PostgreSQL and migrations.** Replace SQLite and the in-process sign-off
   lock. `UNIQUE(run_id)` and `UNIQUE(sequence)` plus a serialisable transaction
   and row lock make concurrent sign-off safe across replicas. Sessions and
   approvals move with the same schema.
2. **Append-only audit tables.** Revoke `UPDATE`/`DELETE` on `events`,
   `signoffs` and `dual_control_approvals` for the application role; hash-chain
   events; restrict governance writes to `run.created`.
3. **KMS/HSM signatures.** Sign the chain payload with a managed asymmetric key
   and record key id and signature. Unkeyed SHA-256 detects accidental edits, not
   a determined database writer.
4. **WORM evidence packs.** Write packs to object storage with retention lock
   and a provider-side digest; keep the stable `pack_id` and per-download
   `pack_hash` contract.
5. **Backups and restore drills.** PITR, encrypted backups, documented restore
   time objective and a tested restore.
6. **Secret and key management.** Move `SESSION_SECRET`, OIDC client secret and
   signing keys to the platform secrets manager; rotate on a schedule.
7. **Real OIDC integration test.** Wire the corporate tenant, prove login,
   group mapping, MFA claim, logout and error paths against the IdP.
8. **Observability and SIEM.** Structured logs without payloads, OTel traces,
   export/sign-off/approval alerts, rate-limit and auth-failure dashboards.
9. **Ingestion lineage.** Signed source extracts, control totals, dataset
   version and an authenticated ingestion path to replace local seeding.
10. **Platform hardening.** Non-root read-only container, network policy,
    ingress TLS/WAF, dependency lockfile and image scanning.

Production data remains blocked until items 1–5 are in place and the Phase 2
exit review is signed.
