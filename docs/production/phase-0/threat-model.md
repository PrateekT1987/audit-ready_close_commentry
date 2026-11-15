# Threat model — Ledgerline production target

**Method:** STRIDE, asset-centric, with attack paths for the highest-risk cases
**Status:** Proposed — pending security review and risk acceptance
**Scope:** single-tenant production deployment described in ADR-001 to ADR-004
**Last updated:** 2026-11-15

## 1. Scope and assumptions

In scope:

- the Ledgerline API, pipeline worker, UI, database, object storage, KMS,
  ingestion boundary and deployment pipeline;
- human users (preparer, reviewer, signer, auditor, admin), source systems,
  the identity provider and cloud operators;
- real financial data and long-retention audit records.

Assumptions:

- TLS terminates at a managed ingress/WAF.
- The identity provider enforces MFA and conditional access.
- PostgreSQL, object storage and KMS are managed services with provider audit
  logs.
- The application is single-tenant; isolation between customers is
  infrastructure isolation (ADR-001).
- The engine remains deterministic; an external model is out of scope until a
  new ADR.

## 2. Assets

| Asset | Why it matters | Owner |
| --- | --- | --- |
| Journal, plan, FX and driver data | Source of every figure; manipulation changes the close | Finance data owner |
| Fact store and derivations | Evidence that each figure ties to the ledger | Engineering |
| Commentary claim plan | What the organization says about the close | Finance controller |
| Verification report and findings | Proof the gate ran and what it found | Engineering / audit |
| Sign-off record and chain | Legal/audit evidence of who approved what | Finance controller |
| Attestations and dual-control approvals | Waiver evidence and governance | Finance controller |
| Evidence packs | What auditors receive and verify offline | Audit |
| Signing keys | Non-repudiation of sign-offs | Security |
| Identities and role mappings | Who can prepare, review and sign | Security |
| Availability of the service | Close calendar is time-critical | Platform owner |

## 3. Actors and entry points

| Actor | Trust | Entry point |
| --- | --- | --- |
| Preparer | Authenticated, authorised | UI/API: create, regenerate, re-verify |
| Reviewer | Authenticated, authorised | UI/API: read, findings, notes |
| Signer | Authenticated, authorised, MFA | UI/API: sign-off |
| Auditor | Authenticated, read-only | UI/API: chain, exports |
| Admin | Authenticated, privileged | Config, roles, break-glass |
| Source system | Service identity | Ingestion API/file drop |
| OIDC provider | Trusted identity authority | OIDC endpoints |
| Cloud operator | Privileged infrastructure | Cloud control plane |
| Attacker | Untrusted | Internet, stolen session, insider, supply chain |
| CI/CD | Privileged automation | Image registry, migration job |

## 4. Risk rating

Likelihood: Almost certain (4), Likely (3), Possible (2), Unlikely (1).
Impact: Critical (4), High (3), Medium (2), Low (1).
Risk = likelihood × impact: 12–16 Critical, 8–9 High, 4–6 Medium, 1–3 Low.

“Current” is the repository as reviewed on 2026-11-15. “Residual” is after the
Phase 1–4 mitigations in this pack.

## 5. Threat register

### Spoofing

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| S-01 | Caller signs as any name | `SignOffRequest.signer` is free text (`app/main.py:148`); no auth | Critical | Critical (16) | OIDC subject, server-derived signer, RBAC, MFA (ADR-002) | Low (2) |
| S-02 | Preparer claims another preparer | `CreateRun.prepared_by` is free text (`app/main.py:127`) | Critical | Critical (16) | Derive preparer from session; store `sub`; ignore body | Low (2) |
| S-03 | Stolen session or token replay | No sessions today; future BFF cookie | High | High (9) | HttpOnly/Secure/SameSite, short idle/absolute life, rotation, IdP revocation, CSRF | Medium (4) |
| S-04 | Audience/issuer confusion or forged token | No OIDC today | High | High (9) | Validate issuer, audience, signature, nonce, JWKS cache; integration tests | Low (3) |
| S-05 | Break-glass/admin impersonation | No admin path today | High | High (9) | Named break-glass accounts, MFA, alerting, post-use review | Medium (4) |

### Tampering

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| T-01 | Rewrite a signed record and recompute the chain | Unkeyed SHA-256; any DB writer can change records | Critical | Critical (16) | KMS/HSM signature over chain payload, immutable audit tables, WORM, external timestamp (ADR-003/004) | Medium (6) |
| T-02 | Edit `run.created` to change preparer/dual control | `events` is mutable; governance reads it (`app/store.py:158`) | Critical | Critical (16) | Revoke UPDATE/DELETE on events; hash-chain events; admin-approved dual control (ADR-002/003) | Low (3) |
| T-03 | Change commentary or verification after signing | Chain re-hashes live artefacts, but DB writer can rewrite both | High | High (9) | Signed artefacts + WORM pack + offline verification; DB role separation | Low (3) |
| T-04 | Concurrent sign-off forks the chain | `next_sequence()` separate from insert; two readers both see `1` (`app/store.py:136`); no unique `run_id` | High | High (9) | One serialisable transaction, `UNIQUE(run_id)`, `UNIQUE(sequence)`, row lock (ADR-003) | Low (2) |
| T-05 | Poisoned or substituted source extract | Dataset loaded from local JSON; missing file is seeded (`app/store.py:176`) | High | High (9) | Immutable dataset version, source hash/control totals, validation, fail closed, ingestion auth | Medium (4) |
| T-06 | Fact derivation repointed at different evidence | Fact hash commits to derivation, which is good | High | Medium (6) | Preserve full derivation in signed pack; restrict fact-store writes | Low (2) |
| T-07 | Policy/materiality changed after sign-off | Hardcoded config; no policy version | High | High (9) | Versioned policy, commit policy hash/id into signed record | Low (3) |

### Repudiation

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| R-01 | Signer denies signing | Name only, no identity proof | Critical | Critical (16) | Signed record with `sub`, KMS signature, RFC 3161 timestamp, immutable event | Low (2) |
| R-02 | “The system clock was wrong” | `signed_at` from host clock | Medium | Medium (6) | Trusted external timestamp; sequence remains order of record | Low (2) |
| R-03 | Export/read denied later | No export access log | Medium | Medium (6) | `audit_exported` event with subject, pack id/hash; SIEM | Low (2) |
| R-04 | Attestation rationale disputed | Structured detail exists, but identity weak | High | High (9) | Bind attestation to signer subject and timestamp | Low (3) |

### Information disclosure

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| I-01 | Anonymous download of financial packs | Every endpoint public; no auth (`app/main.py`) | Critical | Critical (16) | OIDC + RBAC + export authorization + access log | Low (2) |
| I-02 | Tokens stolen by XSS | No tokens today; BFF design avoids browser tokens | High | High (9) | HttpOnly cookie, CSP, output escaping, no token in storage | Low (3) |
| I-03 | Financial data in logs/traces | No logging today; future risk | High | High (9) | Structured redaction; never log payloads, attestation text or tokens | Medium (4) |
| I-04 | Public object-storage bucket | No object storage today | High | High (9) | Private bucket, block public access, WORM, signed access | Low (2) |
| I-05 | Backup exfiltration | Local SQLite; no backup policy | High | High (9) | Encrypted managed backups, separate key, restore access audited | Medium (4) |
| I-06 | Over-broad auditor/admin access | No roles today | High | High (9) | Least privilege, per-endpoint authz, periodic access review | Medium (4) |

### Denial of service

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| D-01 | Anonymous run flood fills the disk | `POST /api/runs` public, ~1 MB/run, ~0.4 s each | High | High (9) | Auth, quotas, rate limit, async queue, retention/archival | Medium (4) |
| D-02 | Huge attestation/body exhausts memory | Attestation fields unbounded | Medium | Medium (6) | Pydantic max lengths, body-size limit, edge limit | Low (2) |
| D-03 | Evidence-pack generation saturates the app | 1.4 MB built in memory per request | Medium | Medium (6) | Stream/offload to object storage, cache signed packs | Low (2) |
| D-04 | Queue backlog during close | No queue today | Medium | Medium (6) | Queue depth alerts, autoscaling, backpressure | Medium (4) |

### Elevation of privilege

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| E-01 | Preparer self-waives separation of duties | `dual_control` is a request boolean (`app/main.py:131`) | Critical | Critical (16) | Admin-approved exception record, second approver, expiry, signed into chain | Low (3) |
| E-02 | Role claim tampering | No roles today | High | High (9) | Server-side mapping from IdP groups; deny by default | Low (3) |
| E-03 | Admin edits a signed run | No admin model today | High | High (9) | No mutation path for signed runs; append-only; admin actions audited | Medium (4) |
| E-04 | SSRF through ingestion URLs | No ingestion today | Medium | Medium (6) | Allowlist, egress policy, no arbitrary URLs | Low (2) |
| E-05 | Container compromise | Single process on host | High | High (9) | Non-root, read-only FS, minimal image, network policy, seccomp | Medium (4) |

### Other / cross-cutting

| ID | Threat | Current evidence | Impact | Current | Mitigation | Residual |
| --- | --- | --- | --- | --- | --- | --- |
| A-01 | Supply-chain dependency compromise | Floating `>=` versions, no lock/scan | High | High (9) | Lockfile, pinned digests, Dependabot, pip-audit, SBOM, signed images | Medium (4) |
| A-02 | Secrets committed or in image | No secrets today; future risk | High | High (9) | Secrets manager, secret scanning, no `.env` in git | Low (3) |
| A-03 | CSRF on sign-off | No auth today; future risk | High | High (9) | CSRF token + Origin check + SameSite (ADR-002) | Low (2) |
| A-04 | XSS through commentary/findings | UI escapes with `esc()`; still large inline HTML surface | Medium | Medium (6) | CSP, keep escaping, security review of every `innerHTML` | Low (3) |
| A-05 | Host-header/proxy misconfiguration | Only GZip middleware | Medium | Medium (6) | TrustedHost, correct proxy headers, TLS redirect | Low (2) |
| A-06 | Stale data accepted as current | No readiness/freshness gate | High | High (9) | Readiness checks dataset period/freshness; refuse stale runs | Low (3) |
| A-07 | Engine/verifier drift misattributed | Versions now stamped, good | Medium | Low (3) | Keep versions in signed record; block unknown versions in policy | Low (2) |
| A-08 | Ransomware/deletion of audit data | Local DB, no WORM/backup | High | High (9) | WORM archive, immutable backups, restore drills, least privilege | Medium (4) |
| A-09 | Excessive retention of personal data | No retention policy | Medium | Medium (6) | Retention schedule, minimisation, access review; preserve audit legal hold | Medium (4) |

## 6. Highest-risk attack paths

### Path 1 — Prepare and sign your own close

1. Caller posts a run with `prepared_by: "A. Mensah"` and `dual_control: true`.
2. Caller posts sign-off with `signer: "A. Mensah"`.
3. Current code accepts it; the chain records a name, not an identity.

Mitigation: remove caller-supplied identity, enforce `signer_subject !=
preparer_subject`, require an admin-approved dual-control record, and sign with
KMS. This path becomes impossible without a second authenticated approver.

### Path 2 — Rewrite the audit trail

1. Attacker obtains database write access (backup, misconfigured role, insider).
2. Edits a signed payload, recomputes SHA-256 hashes and rewrites `chain_hash`.
3. Re-verification passes because the hash is unkeyed.

Mitigation: KMS/HSM signature, runtime role without UPDATE/DELETE, WORM pack,
external timestamp. The attacker also needs the non-exportable key, and the
offline verifier detects the mismatch.

### Path 3 — Exfiltrate the close

1. Unauthenticated caller lists runs and downloads evidence packs.
2. Packs contain the full fact store, commentary and control results.

Mitigation: OIDC + RBAC, export authorization, access logging, private object
storage, no public bucket. Auditors still get read-only access by design.

## 7. Accepted risks for Phase 1

| Risk | Acceptance | Review |
| --- | --- | --- |
| Single region / single cloud | Business continuity via backups and documented RTO; multi-region deferred | Phase 5 |
| Legacy sign-offs lack `sub` | Keep chain valid; label as “legacy identity: name only”; no re-signing | At retention review |
| Deterministic narration, no external model | No model-supply-chain risk today; new ADR required before adding one | When model added |
| IdP compromise | Delegated to corporate identity program; break-glass audited | Annual |
| Cloud provider compromise | Shared-responsibility model; provider audit logs and encryption | Annual |

## 8. Security requirements traceability

| Requirement | Threat IDs | Phase |
| --- | --- | --- |
| OIDC, MFA, server-derived identity, RBAC | S-01, S-02, S-03, S-04, E-02 | 1 |
| Admin-approved dual control | E-01 | 1 |
| CSRF, security headers, trusted host, rate limit | S-03, A-03, A-05, D-01 | 1 |
| Input bounds and consistent errors | D-02 | 1 |
| Atomic sign-off, unique constraints, append-only roles | T-02, T-04, R-01 | 2 |
| KMS signatures, timestamp, WORM packs | T-01, T-03, R-01, R-02, A-08 | 2 |
| Fail-closed dataset, immutable versions, freshness | T-05, A-06 | 2 |
| Async queue, quotas, streaming exports | D-01, D-03, D-04 | 3 |
| Logging, metrics, SIEM, export audit | R-03, I-03, I-06 | 3 |
| Lockfile, scanning, SBOM, signed images | A-01, A-02 | 4 |
| Retention and access review | I-06, A-09 | 5 |

## 9. Review and maintenance

- Review this model before each major release and after any incident.
- Re-score when the deployment model, identity provider, data store or signing
  design changes.
- Add a threat whenever a new data source, model or export path is introduced.
- Track accepted risks in the risk register with an owner and expiry.
- Test the top paths in CI or a staging exercise: self-sign attempt, chain
  tamper, unauthorized export, concurrent sign-off, stale dataset.
