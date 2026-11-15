# ADR-001 — Deployment model: single-tenant internal deployment

- **Status:** Proposed
- **Date:** 2026-11-15
- **Deciders:** Product owner, Finance controller, Security, Platform owner
- **Supersedes:** none

## Context

Ledgerline is built around one close: one group, one chart of accounts, one
materiality policy, one FX table and one sign-off chain. Configuration is
currently hardcoded in `app/config.py` and the README states the product is not
a multi-tenant deployment.

The data is confidential financial information. Audit records are
integrity-critical: a signed record must remain attributable and verifiable for
the retention period. Multi-tenancy would add tenant isolation, cross-tenant
authorization, noisy-neighbour controls, per-tenant keys, billing and a much
larger threat surface before the core audit product is hardened.

## Decision

Adopt **single-tenant internal deployment**.

- One deployment serves one finance group (one customer/engagement).
- Each environment (`dev`, `staging`, `prod`) is a separate deployment with its
  own database, object-storage bucket, KMS key and secrets.
- Isolation between customers is infrastructure isolation, not a `tenant_id`
  column: separate accounts/projects, networks, databases and keys.
- No tenant identifier is added to the v1 data model. A future shared service
  requires a new ADR and a migration plan.
- The deployment is internal: users reach it through the corporate identity
  provider and network controls; it is not exposed as a public SaaS product.

## Consequences

Positive:

- Simple authorization model: roles are scoped to the deployment.
- Simple schema and chain: one sequence, one chain head, one policy.
- Data residency and key separation are easy to explain to auditors.
- No cross-tenant data-leak class of bug in Phase 1.

Negative:

- One deployment per customer increases operational cost and upgrade effort.
- Shared operational dashboards need per-deployment aggregation.
- A future multi-tenant offering requires schema, authz and key-model changes.

## Alternatives considered

| Alternative | Why rejected for Phase 0 |
| --- | --- |
| Multi-tenant SaaS now | Adds isolation, billing and compliance scope before the audit core is hardened; no current requirement |
| Shared instance with `tenant_id` | Highest-risk option: one authorization mistake exposes another group's close |
| Single shared deployment for all environments | Blurs data boundaries and key separation; unacceptable for audit evidence |

## Security requirements derived from this decision

- Production, staging and development must not share a database, bucket or key.
- Production data must not be copied to lower environments without an approved
  masking process.
- Backups and evidence archives inherit the production key and retention policy.
- A deployment inventory must record owner, environment, data classification,
  key identifier and retention period.
- Any future tenant model must add row-level security, per-tenant keys and
  cross-tenant authorization tests before launch.

## Review trigger

Revisit this ADR if any of the following becomes true:

- more than one finance group must share an instance;
- the product is offered outside the organization;
- per-customer operating cost becomes material;
- a customer requires data co-location in a region the platform cannot provide.
