# ADR-003 — Data store: managed PostgreSQL with an append-only audit schema

- **Status:** Accepted by decision owner — 2026-11-15
- **Date:** 2026-11-15
- **Deciders:** Platform owner, Security, Finance controller
- **Supersedes:** none

## Context

The current store is a single SQLite file (`data/close.db`) accessed with a new
connection per call. Every connection re-runs the schema with
`executescript(SCHEMA)` (`app/store.py:53`). Measured behaviour:

- `journal_mode=delete`, `foreign_keys=0`, `busy_timeout=5000`;
- 25 MB for 23 runs, about 1 MB per run;
- `next_sequence()` reads every sign-off in a separate transaction, then the
  sign-off is written in another transaction, then the run status in a third;
- two readers can both receive sequence `1`;
- `signoffs` has no `UNIQUE(run_id)` constraint;
- `events` is a normal mutable table even though governance reads it as
  append-only.

SQLite is acceptable for a single-user demo. It is not an acceptable system of
record for concurrent close teams, long retention or a tamper-evident chain.

## Decision

Use **managed PostgreSQL 16** as the system of record.

### Platform requirements

- Managed HA instance with automated backups and point-in-time recovery.
- TLS required for every connection; encryption at rest with a customer-managed
  key where the platform supports it.
- Separate database and credentials per environment.
- Connection pooling (PgBouncer or the platform pooler); no per-request
  `CREATE TABLE`.
- Alembic migrations, applied by CI/CD before application rollout; the app runs
  with a least-privilege runtime role that cannot alter schema.

### Schema shape

| Table | Purpose | Notes |
| --- | --- | --- |
| `runs` | Run identity and lifecycle | `id uuid`, status, period, mode, version, timestamps, lock version |
| `run_artifacts` | Recon, commentary, verification snapshots | `JSONB` per stage, immutable version per narration |
| `facts` | Re-derivable fact store | Queryable columns plus `derivation JSONB` |
| `variances` | Variance queue | Entity/account/period indexes |
| `findings` | Verification findings | Severity/category indexes |
| `signoffs` | Signed records | `UNIQUE(run_id)`, `UNIQUE(sequence)`, exact canonical JSON retained |
| `signoff_attestations` | Structured waivers | One row per warning, details `JSONB` |
| `events` | Append-only event stream | `REVOKE UPDATE, DELETE` from runtime role |
| `policies` | Materiality, roles, dual-control approvals | Versioned, effective dates |
| `dataset_versions` | Immutable source extracts | Hash, period, ingestion timestamp, source refs |
| `audit_exports` | Evidence-pack downloads | Who, when, pack id/hash, object-storage key |

Engine artefacts stay canonical JSON in `JSONB` so the existing hashes remain
reproducible; queryable fields are extracted into columns for listing and
filtering, not used as the source of truth.

### Atomic sign-off

The sign-off must be one transaction:

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE;
-- lock the chain tail
SELECT sequence, chain_hash FROM signoffs ORDER BY sequence DESC LIMIT 1 FOR UPDATE;
-- allocate sequence, insert sign-off, attestations and event, update run
COMMIT;
```

Requirements:

- `UNIQUE(run_id)` prevents two signatures for one run.
- `UNIQUE(sequence)` prevents a forked chain.
- A failed transaction leaves no sign-off, no run status change and no event.
- The runtime role cannot `UPDATE` or `DELETE` sign-offs or events.
- A separate migration/backfill role may insert historical records only under
  change control.

### Audit durability

- Sign-offs and evidence packs are also written to object storage with Object
  Lock/WORM for the retention period.
- Database backups and WORM archives use separate keys and access policies.
- Hash chain remains the internal integrity check; KMS-backed signatures provide
  non-repudiation (see ADR-004).
- Retention default: seven years or the engagement's regulatory policy;
  partition large tables by year and archive closed periods.

### Migration from SQLite

1. Freeze writes to the pilot database.
2. Export every run, sign-off and event with exact canonical JSON.
3. Import into PostgreSQL without re-canonicalising values; verify each
   signature/chain hash after import.
4. Keep the SQLite file as a read-only archive for the pilot period.
5. Do not re-sign legacy records to add `sub`; mark them as legacy identity.

## Consequences

Positive:

- Concurrent sign-off is correct and serialisable.
- Backups, PITR and retention are platform capabilities, not custom scripts.
- Append-only permissions can be enforced by the database role.
- Queryable runs/facts without parsing a 1 MB JSON blob on every request.

Negative:

- Operational dependency and cost.
- Migration and dual-running during cutover.
- JSONB payloads still need retention/compaction discipline.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Keep SQLite with WAL and a mutex | Single-writer, weak HA, no PITR, hard to enforce append-only |
| Document database | Weaker relational constraints for chain/sequence invariants |
| Event-store product | Attractive long term, but too large a change before the audit core is hardened |
| Files on disk | No transactional guarantees or queryability |

## Security requirements derived from this decision

- Enable `foreign_keys` and a least-privilege runtime role.
- Revoke `UPDATE`/`DELETE` on `signoffs` and `events` from the app role.
- Require TLS and verify the server certificate.
- Store the database password in a secrets manager, never in `.env` in git.
- Test backup restore and PITR before go-live.
- Add concurrency tests: N parallel sign-offs produce exactly one valid chain.
