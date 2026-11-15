# Phase 0 sign-off record

**Status:** Accepted — Phase 1 authorised (decision owner, 2026-11-15)
**Date opened:** 2026-11-15
**Decision pack:** [README](README.md), [ADRs](adr/), [data flow](data-flow.md), [threat model](threat-model.md)

Phase 0 is closed for the four architecture decisions. The decision owner
accepted the pack on 2026-11-15. Named Security, Finance and Platform
acknowledgements are recorded at Phase 1 kickoff; they do not reopen the
architecture choice unless a superseding ADR is raised.

## 1. Decisions

| ID | Decision | Proposed choice | Decision | Owner |
| --- | --- | --- | --- | --- |
| ADR-001 | Deployment model | Single-tenant internal deployment, separate infrastructure per environment | ☑ Accept ☐ Change | Product owner |
| ADR-002 | Identity and session | Corporate OIDC (reference Entra ID), BFF session, MFA, RBAC, server-derived signer/preparer | ☑ Accept ☐ Change | Security |
| ADR-003 | Data store | Managed PostgreSQL 16, Alembic, serialisable sign-off, append-only audit tables, PITR | ☑ Accept ☐ Change | Platform owner |
| ADR-004 | Runtime and platform | Managed containers, TLS ingress, WORM object storage, KMS/HSM signatures, OTel, CI/CD | ☑ Accept ☐ Change | Platform owner |

If a decision is changed, add a superseding ADR and update the table above with a
link. Do not edit an accepted ADR in place.

## 2. Review checklist

| # | Check | Evidence | Reviewer | Done |
| --- | --- | --- | --- | --- |
| 1 | Deployment model matches the operating model and customer contract | ADR-001 | Product owner | ☐ |
| 2 | Identity provider, MFA and role mapping are available | ADR-002 | Security / IT | ☐ |
| 3 | Separation of duties and dual-control approval are acceptable to Finance | ADR-002, threat model E-01 | Finance controller | ☐ |
| 4 | PostgreSQL platform, backup and PITR meet RPO/RTO | ADR-003 | Platform owner | ☐ |
| 5 | KMS/HSM and WORM retention meet audit/legal requirements | ADR-004 | Security / Audit | ☐ |
| 6 | Data-flow model is accurate for source systems and retention | `data-flow.md` | Finance data owner | ☐ |
| 7 | Threat model is reviewed and residual risks are accepted | `threat-model.md` §5, §7 | Security | ☐ |
| 8 | Phase 1 scope, owner and target dates are agreed | section 3 below | All | ☐ |
| 9 | No production data will be loaded before Phase 2 exit | ADR-001, ADR-003 | Product owner | ☐ |
| 10 | Budget/capacity for managed Postgres, KMS and WORM is approved | ADR-003/004 | Finance / Platform | ☐ |

Items 2–7 and 10 are Phase 1 kickoff gates for the named owners: they confirm
implementation readiness, budgets and control ownership. They do not reopen the
architecture choice, which the decision owner has accepted.

## 3. Phase 1 scope approved by this sign-off

Approved 2026-11-15. Phase 1 delivers the security perimeter:

- OIDC authorization code + PKCE with BFF session, CSRF and logout;
- server-side role mapping and per-endpoint authorization;
- remove `signer` and `prepared_by` from request bodies;
- server-derived preparer/signer subjects; enforce `signer != preparer`;
- admin-approved dual-control exception record;
- security headers, trusted hosts, explicit CORS, body limits, rate limits;
- period validation and bounded request fields;
- consistent error envelope with request ids;
- disable/protect `/docs`, `/openapi.json` and sensitive audit endpoints;
- authorization, CSRF and input-validation tests.

Phase 1 does **not** load production data. That waits for Phase 2 (PostgreSQL,
atomic sign-off, KMS signatures, backups, readiness) and a successful restore
drill.

| Workstream | Owner | Target date |
| --- | --- | --- |
| OIDC/BFF and RBAC | | |
| Separation of duties and dual control | | |
| Edge and API hardening | | |
| Test coverage and CI gate | | |

## 4. Risk acceptance

The following risks are accepted for Phase 1, with owners and review dates:

| Risk | Owner | Accepted until | Review date |
| --- | --- | --- | --- |
| Single region / single cloud | Platform owner | Phase 5 | |
| Legacy sign-offs lack `sub` | Finance controller | Retention review | |
| Deterministic narration only | Product owner | Model ADR | |
| IdP compromise delegated to corporate identity | Security | Annual | |
| Cloud provider compromise under shared responsibility | Security | Annual | |

Residual risks from the threat model are recorded in §7 of
[`threat-model.md`](threat-model.md). Any risk rated High or Critical after
mitigation needs an explicit owner above.

## 5. Signatures

| Role | Name | Decision | Signature / approval | Date |
| --- | --- | --- | --- | --- |
| Product owner / repository owner | Repository owner (session approval) | ☑ Approve ☐ Reject | Accepted the four ADRs in the Phase 0 review | 2026-11-15 |
| Finance controller | | ☐ Approve ☐ Reject | Pending Phase 1 kickoff | |
| Security lead | | ☐ Approve ☐ Reject | Pending Phase 1 kickoff | |
| Platform owner | | ☐ Approve ☐ Reject | Pending Phase 1 kickoff | |
| Finance data owner | | ☐ Approve ☐ Reject | Pending Phase 1 kickoff | |

Electronic approval in the pull request that adds this file is acceptable;
record the approver, date and commit SHA here.

**Approval evidence:** decision-owner acceptance recorded 2026-11-15; decision
pack commit `468ada2`, acceptance commit `1928df9`. The remaining named
acknowledgements are Phase 1 kickoff gates, not blockers to starting the
provider-agnostic security work.

## 6. Change log

| Date | Change | By | Commit |
| --- | --- | --- | --- |
| 2026-11-15 | Initial Phase 0 decision pack created | Engineering | `468ada2` |
| 2026-11-15 | Four ADRs and Phase 1 scope accepted by decision owner | Repository owner | `1928df9` |
