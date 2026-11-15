# ADR-002 — Identity, session and separation of duties

- **Status:** Accepted by decision owner — 2026-11-15
- **Date:** 2026-11-15
- **Deciders:** Security, Finance controller, Platform owner
- **Supersedes:** none

## Context

The current API has no authentication. `CreateRun.prepared_by` and
`SignOffRequest.signer` are free-text fields supplied by the caller
(`app/main.py:127`, `app/main.py:148`). A caller can claim to be the preparer,
set `dual_control: true` themselves and sign their own run. The
`run.created` event is described as the system of record for governance, but it
is a mutable database row written from unauthenticated input.

Financial sign-off requires attributable identity, MFA, role-based access and
separation of duties that cannot be waived by the signer.

## Decision

Use **corporate OIDC with a backend-for-frontend (BFF) session**.

### Provider

- Reference provider: **Microsoft Entra ID** (common in finance organizations).
- Supported alternatives: Okta, Google Workspace, Keycloak (on-prem), Auth0.
- No local password store. MFA and conditional access are enforced by the IdP.
- The integration uses Authlib (or an equivalent maintained OIDC client), not a
  custom token parser.

### Flow

1. Browser starts authorization code + PKCE against the IdP.
2. FastAPI handles the callback, validates `iss`, `aud`, signature, `exp`,
   `nbf`, `nonce` and `state`, then creates a server-side session.
3. The browser receives an opaque session cookie: `HttpOnly`, `Secure`,
   `SameSite=Lax`, `Path=/`, no tokens in `localStorage`.
4. The server holds access/refresh tokens; the browser never sees them.
5. Idle timeout 8 hours, absolute session lifetime 12 hours, rotation on login,
   revocation on logout and on IdP disablement.

### Roles

Roles come from IdP groups/claims, mapped server-side. Display names are labels;
the immutable subject (`iss` + `sub`) is the identity of record.

| Role | Permissions |
| --- | --- |
| `preparer` | Create runs, regenerate commentary, re-verify, read workpapers |
| `reviewer` | Read workpapers, open findings, record review notes; cannot sign |
| `signer` | Sign a run after the gate passes; may also review |
| `auditor` | Read-only access to runs, chain and evidence packs; no mutations |
| `admin` | Manage policy/configuration and role mapping; cannot silently edit a signed run |

A user may hold several roles. Authorization is checked per endpoint and per
run, not only at login.

### Separation of duties

- The preparer is the authenticated subject who created the run. It is stored
  as `preparer_subject` and snapshotted into the signed record.
- The signer is the authenticated subject calling sign-off. The API no longer
  accepts `signer` or `prepared_by` in request bodies.
- Signing is refused when `signer_subject == preparer_subject`.
- A dual-control exception is an **admin-approved policy record** with a reason,
  a second approver and an expiry. It is committed into the chain; it is not a
  boolean supplied by the caller.
- The same subject may not act as both reviewer and signer for the same run if
  the engagement policy requires independent review.

### Request protection

- State-changing requests require a CSRF token in addition to the session
  cookie, plus an `Origin`/`Referer` check.
- `/docs`, `/openapi.json` and the audit chain are disabled or restricted to
  `admin`/`auditor` in production.
- Ingestion uses a service identity (client credentials or mTLS), never a human
  account.

### Audit fields

Every event and sign-off records:

- `iss`, `sub`, `email`, `display_name`, `roles`, `auth_time`, `session_id`;
- the policy/approval identifier when dual control is used;
- the client IP and user agent at the edge, treated as supporting evidence, not
  identity.

## Consequences

Positive:

- Sign-off is attributable to an authenticated person, not a typed name.
- MFA, conditional access and offboarding are delegated to the IdP.
- Separation of duties cannot be self-waived.

Negative:

- The application depends on IdP availability; a break-glass admin path is
  required and must be audited.
- Group/claim mapping must be maintained and tested.
- Existing signed records remain valid but do not carry `sub`; the certificate
  must show "legacy identity: name only" for them.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Keep typed names | No attribution, trivially spoofed, fails audit |
| Local username/password | Adds password storage, reset, MFA and breach scope |
| Tokens in browser storage | XSS can exfiltrate them; BFF keeps tokens server-side |
| API keys for users | Not attributable to a person and awkward to rotate/offboard |

## Security requirements derived from this decision

- Remove `signer` and `prepared_by` from public request schemas.
- Reject sign-off when the authenticated subject is the preparer, unless a
  valid admin-approved exception is present.
- Store the subject, not just the display name, on every sign-off.
- Add authorization tests for every role and every endpoint.
- Add a break-glass procedure with alerting and post-incident review.
