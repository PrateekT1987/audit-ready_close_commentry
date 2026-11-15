# ADR-004 — Runtime platform and cryptographic trust

- **Status:** Proposed
- **Date:** 2026-11-15
- **Deciders:** Platform owner, Security, Finance controller
- **Supersedes:** none

## Context

The current runtime is a single Uvicorn process started by `run.sh`, writing to
a local SQLite file on the same host. There is no container image, CI/CD,
reverse proxy, TLS, worker model, secret manager or backup. Signed records use
unkeyed SHA-256: anyone who can write the database can rewrite a record and
recompute the chain.

Production needs a deployable, observable runtime and a signing key the
application can use but cannot export.

## Decision

Run Ledgerline as a **containerised service on a managed container platform**,
with managed data services and a KMS/HSM-backed signing key.

### Reference architecture

```text
Internet / corporate network
        │  TLS 1.2+
        ▼
WAF / load balancer / ingress  ──►  OIDC provider
        │
        ▼
Ledgerline container(s)  ──►  managed PostgreSQL 16
        │                ──►  job queue (run pipeline)
        │                ──►  object storage (WORM evidence packs)
        │                ──►  KMS/HSM (signing, encryption keys)
        │                ──►  secrets manager
        │                ──►  OTel collector / logs / metrics
        ▼
Auditors (read-only)
```

### Provider mapping

The decision is platform-neutral; the organization's primary cloud selects the
mapping. The reference implementation is the first column.

| Capability | AWS reference | Azure reference | GCP reference | On-prem reference |
| --- | --- | --- | --- | --- |
| Container runtime | ECS/Fargate | Container Apps | Cloud Run | Kubernetes |
| PostgreSQL | RDS/Aurora PostgreSQL | Flexible Server | Cloud SQL | CloudNativePG |
| Object storage + WORM | S3 Object Lock | Blob immutable storage | GCS retention lock | MinIO object lock |
| KMS/HSM | KMS + CloudHSM | Key Vault Managed HSM | Cloud KMS | Vault + HSM |
| Secrets | Secrets Manager | Key Vault | Secret Manager | Vault |
| Queue | SQS | Service Bus | Pub/Sub | RabbitMQ/NATS |
| Observability | CloudWatch + OTel | Monitor + OTel | Cloud Monitoring + OTel | Prometheus/Grafana/Loki |

### Runtime requirements

- Immutable container image, non-root user, read-only root filesystem, no shell
  in production, pinned base image digest.
- At least two replicas behind the ingress; autoscaling on request concurrency.
- Liveness and readiness probes; readiness checks database, dataset freshness,
  chain integrity and KMS availability.
- Synchronous run creation is replaced by a queue: `POST /api/runs` returns
  `202` with a job id; the worker performs reconcile → narrate → verify.
- Graceful shutdown drains in-flight requests and returns jobs to the queue.
- Request and job timeouts; per-principal quotas; evidence-pack generation
  streamed or offloaded to object storage.
- Egress restricted to the IdP, database, object storage, KMS, secrets manager
  and approved source systems.

### Cryptographic trust

The hash chain proves internal consistency, not authorship. Add:

- an **Ed25519 or ECDSA P-256** signature over the canonical sign-off chain
  payload, produced by a KMS/HSM key the app cannot export;
- a `key_id` and algorithm identifier in every signed record;
- public keys retained for the full retention period; rotation creates a new
  key id, never rewrites old records;
- an RFC 3161 timestamp or transparency-log anchor for each sign-off, so the
  signing time is externally witnessed;
- evidence packs written to WORM storage with a manifest containing the pack id,
  pack hash, sign-off id and key id;
- an offline verifier that needs only the pack, the public keys and the
  timestamp token — not the production database.

### Environments and delivery

- `dev`, `staging`, `prod` in separate accounts/projects with separate databases,
  buckets, keys and IdP applications.
- CI: lint, type-check, unit/integration tests, coverage gate, dependency and
  container scanning, SBOM, image signing.
- CD: immutable image promotion; staging smoke test; manual approval for prod;
  database migrations applied before rollout with a rollback plan.
- No production data in dev/staging. No long-lived cloud credentials in CI;
  use workload identity/OIDC federation.

### Observability

- Structured JSON logs with request id, run id, subject id (never tokens,
  attestation free text or full financial payloads).
- Metrics: request latency/error rate, run duration, queue depth, sign-off
  failures, chain verification status, database connections, KMS errors.
- Traces across API → queue → worker → database.
- Alerts: chain verification failure, sign-off refusal spike, readiness failure,
  KMS unavailable, backup failure, disk/queue pressure.
- Audit-log export to the SIEM, separate from application logs.

## Consequences

Positive:

- Deployable, observable and recoverable with platform primitives.
- Sign-off becomes non-repudiable even if the database is compromised.
- Expensive work is isolated from the request path.
- Evidence can be verified offline by an auditor.

Negative:

- More moving parts and platform cost.
- Key rotation, timestamp anchoring and WORM retention need operational runbooks.
- The queue adds asynchronous UX and idempotency requirements.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Single VM with Docker Compose | Acceptable for a pilot, not for HA, PITR, WORM or key custody |
| Serverless functions only | Time limits and cold starts conflict with the multi-stage engine; queue/worker still needed |
| Kubernetes everywhere | Valid if the organization already operates it; otherwise too much platform burden |
| SHA-256 only | Tamper-evident but not tamper-proof; a database writer can rewrite the chain |

## Security requirements derived from this decision

- TLS termination with HSTS; HTTP redirected to HTTPS.
- WAF/rate limiting at the edge; request body limits.
- Security headers: CSP, `X-Content-Type-Options`, `Referrer-Policy`,
  frame-ancestors, permissions policy.
- Trusted-host allowlist and explicit CORS allowlist.
- Secrets from the secrets manager; no secrets in images or git.
- KMS key policy restricted to the runtime role and the offline verifier.
- Backup, restore, key-rotation and incident-response drills before go-live.
