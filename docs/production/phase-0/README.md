# Phase 0 — Architecture decisions and threat model

**Status:** Proposed — awaiting sign-off
**Owner:** Architecture / Security
**Applies to:** Ledgerline (`app/`), production deployment
**Last updated:** 2026-11-15

Phase 0 fixes the deployment shape, identity model, data store and runtime
platform before any production hardening code is written. The exit criterion is
a signed-off decision pack, not a running production system.

## Decisions at a glance

| ID | Decision | Proposed choice | Status |
| --- | --- | --- | --- |
| ADR-001 | Deployment model | Single-tenant internal deployment, one instance per finance group and environment | Proposed |
| ADR-002 | Identity and session | Corporate OIDC (reference: Microsoft Entra ID), authorization code + PKCE, backend-for-frontend session cookie, MFA at the IdP | Proposed |
| ADR-003 | System of record | Managed PostgreSQL 16 with Alembic migrations, PITR and append-only audit tables | Proposed |
| ADR-004 | Runtime platform | Managed containers behind TLS ingress, managed Postgres, WORM object storage, KMS/HSM, secrets manager, OTel observability | Proposed |

The choices are deliberately conservative: they remove the demo's largest
production risks (self-declared identity, race-prone SQLite chain, mutable
audit tables) without committing to multi-tenant product scope.

## Deliverables

- [ADR-001 — Deployment model](adr/001-deployment-model.md)
- [ADR-002 — Identity and session](adr/002-identity-and-session.md)
- [ADR-003 — Data store](adr/003-data-store.md)
- [ADR-004 — Runtime and platform](adr/004-runtime-and-platform.md)
- [Data-flow model](data-flow.md)
- [Threat model](threat-model.md)
- [Sign-off record](signoff.md)

## Scope and assumptions

In scope:

- one finance group, one close dataset per period, one audit chain;
- internal controllers, reviewers, auditors and administrators;
- confidential financial data, identity data and integrity-critical audit records;
- a production deployment that may hold real ledger, plan and FX extracts.

Out of scope for Phase 0:

- multi-tenant isolation and tenant billing;
- replacing the deterministic narration template with an external model;
- implementing the hardening work itself (Phases 1–4);
- source-system connectors beyond the ingestion contract described in the
  data-flow model.

## What must be true before sign-off

1. The deployment model is accepted (single-tenant vs shared).
2. The identity provider and role mapping are accepted.
3. PostgreSQL and the migration approach are accepted.
4. The runtime platform and cloud/provider mapping are accepted.
5. The data-flow model is reviewed by the finance data owner.
6. The threat model is reviewed by security, and residual risks are accepted.
7. Phase 1 scope and owners are recorded in the sign-off record.

## Changing a decision

An accepted ADR is changed by a new superseding ADR, not by editing history.
Record the new decision, the reason, the migration impact and the effective
date in `signoff.md`.

## Relationship to the production roadmap

| Phase | Depends on |
| --- | --- |
| Phase 1 — Security perimeter | ADR-001, ADR-002 |
| Phase 2 — Durable core | ADR-003, ADR-004 |
| Phase 3 — Scale and operability | ADR-003, ADR-004 |
| Phase 4 — Delivery and assurance | ADR-004 |
| Phase 5 — Data product hardening | ADR-001, ADR-003 |
