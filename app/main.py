"""FastAPI application: the close pipeline as a service.

Pipeline:  reconcile -> narrate -> verify -> (human) sign-off

Every route is authenticated and authorized except liveness, readiness and the
static shell. Identity, role and separation-of-duties evidence are derived from
the authenticated session, never from a request body.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app import auth, config, store
from app.auth import Principal, require
from app.auth_routes import configure_oidc
from app.auth_routes import router as auth_router
from app.engine import audit
from app.engine.diff import diff_runs
from app.engine.narrate import MODE_LABELS, narrate
from app.engine.reconcile import reconcile
from app.engine.rollforward import build_rollforward, dataset_for_period
from app.engine.verify import verify
from app.security import ApiError, SecurityMiddleware, error_response, request_id_for
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ledgerline.api")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Ledgerline — Audit-Ready Close Commentary",
    version="1.1.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

# Middleware executes outer-to-inner in reverse registration order: the
# security middleware is added last so every response, including CORS
# refusals, carries a request id and the security headers.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Pack-ID", "X-Pack-Hash"],
        max_age=600,
    )
app.add_middleware(GZipMiddleware, minimum_size=800)
if settings.oidc_enabled:
    # Transient signed cookie used by Authlib for OIDC state/nonce only.
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="ledgerline_oidc_state",
        max_age=600,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
app.add_middleware(SecurityMiddleware)
app.include_router(auth_router)
if settings.oidc_enabled:
    configure_oidc()

_SIGNOFF_LOCK = threading.Lock()
_dataset_cache: dict | None = None


# ---------------------------------------------------------------- data access
def dataset() -> dict:
    global _dataset_cache
    if _dataset_cache is None:
        try:
            _dataset_cache = store.load_dataset(allow_seed=settings.allow_dataset_seed)
        except store.DatasetUnavailable as exc:
            raise ApiError(503, "dataset_unavailable", str(exc)) from exc
    return _dataset_cache


def _dataset_for_period(period: str) -> dict:
    try:
        return dataset_for_period(dataset(), period)
    except (KeyError, ValueError) as exc:
        raise ApiError(422, "validation_error", f"Unknown period: {period}.") from exc


def _status(verification: dict, signed: bool) -> str:
    if signed:
        return "signed"
    if verification["gate"]["status"] == "blocked":
        return "blocked"
    if verification["gate"]["status"] == "attestation":
        return "attestation"
    return "clear"


def _slim_facts(facts: list[dict]) -> list[dict]:
    out = []
    for fact in facts:
        derivation = dict(fact["derivation"])
        derivation.pop("lines", None)
        out.append({**fact, "derivation": derivation})
    return out


def _chain_state() -> tuple[list[dict], list[dict]]:
    signoffs = store.load_signoffs()
    runs = {}
    for record in signoffs:
        run = store.load_run(record["run_id"])
        if run is not None:
            runs[record["run_id"]] = run
    return signoffs, audit.verify_chain(signoffs, runs)


def _approval_summary(approval: dict | None) -> dict | None:
    if approval is None:
        return None
    return {
        "approval_id": approval["approval_id"],
        "approved_by": approval["approved_by"],
        "approved_subject": approval["approved_subject"],
        "requested_by": approval["requested_by"],
        "requested_subject": approval["requested_subject"],
        "reason": approval["reason"],
        "created_at": approval["created_at"],
        "expires_at": approval["expires_at"],
    }


def _run_view(run: dict, principal: Principal) -> dict:
    payload = run["payload"]
    recon = payload["recon"]
    commentary = payload["commentary"]
    verification = payload["verification"]
    signoffs = store.load_signoffs(run["id"])
    signoff = signoffs[-1] if signoffs else None
    governance = store.run_governance(run["id"])
    approval = store.active_dual_control(run["id"])
    state = audit.preflight(recon, verification, [])
    _, chain = _chain_state()
    signed = run["status"] == "signed"

    return {
        "run": {
            "id": run["id"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "status": run["status"],
            "period": run["period"],
            "period_label": config.PERIOD_LABELS.get(run["period"], run["period"]),
            "mode": run["mode"],
            "mode_label": MODE_LABELS.get(run["mode"], run["mode"]),
            "label": run["label"],
            "preparer": governance["preparer"],
            "preparer_subject": governance["preparer_subject"],
            "preparer_issuer": governance["preparer_issuer"],
            "dual_control": bool(approval) or bool(governance["dual_control"]),
            "dual_control_approval": _approval_summary(approval),
            "engine_version": recon.get("engine_version") or config.ENGINE_VERSION,
            "verifier_version": verification.get("verifier_version") or config.VERIFIER_VERSION,
        },
        "viewer": auth.viewer_context(
            principal,
            preparer_subject=governance["preparer_subject"],
            signed=signed,
            approval=approval,
        ),
        "versions": {
            "engine": recon.get("engine_version") or config.ENGINE_VERSION,
            "verifier": verification.get("verifier_version") or config.VERIFIER_VERSION,
            "evidence_pack": config.EVIDENCE_PACK_VERSION,
        },
        "recon": {
            **{k: v for k, v in recon.items() if k != "facts"},
            "facts": _slim_facts(recon["facts"]),
        },
        "commentary": commentary,
        "verification": verification,
        "preflight": state,
        "signoff": signoff,
        "chain": chain,
        "events": store.load_events(run["id"]),
    }


def _execute(period: str, mode: str) -> dict:
    raw = _dataset_for_period(period)
    recon = reconcile(raw)
    commentary = narrate(recon, mode=mode)
    verification = verify(raw, recon, commentary)
    return {"recon": recon, "commentary": commentary, "verification": verification}


def _load_run_or_404(run_id: str) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise ApiError(404, "not_found", "Run not found.")
    return run


# --------------------------------------------------------------- request models
class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: str | None = Field(default=None, max_length=10)
    mode: str = Field(default="strict", pattern="^(strict|drift)$")


class RenarrateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(default="strict", pattern="^(strict|drift)$")


class Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str = Field(min_length=1, max_length=120)
    rationale: str = Field(default="", max_length=2000)
    control_id: str | None = Field(default=None, max_length=200)
    remediation_owner: str | None = Field(default=None, max_length=200)
    remediation_date: str | None = Field(default=None, max_length=40)
    reviewed_by: str | None = Field(default=None, max_length=200)
    details: dict[str, str] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _bounded_details(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("details may contain at most 20 entries")
        for key, item in value.items():
            if len(str(key)) > 64 or len(str(item)) > 500:
                raise ValueError("details keys must be <= 64 and values <= 500 characters")
        return value


class SignOffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)
    attestations: list[Attestation] = Field(default_factory=list, max_length=50)


class DualControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=10, max_length=1000)
    expires_at: datetime | None = None


# ------------------------------------------------------------------ auth state
@app.get("/api/health")
def health() -> dict:
    """Liveness only: no database, dataset or secret is touched."""
    return {
        "ok": True,
        "service": "ledgerline",
        "version": app.version,
        "engine_version": config.ENGINE_VERSION,
        "verifier_version": config.VERIFIER_VERSION,
        "evidence_pack_version": config.EVIDENCE_PACK_VERSION,
        "environment": settings.app_env,
    }


@app.get("/api/ready")
def ready() -> Response:
    """Readiness: database, dataset contract/freshness and chain integrity."""
    checks: dict[str, dict] = {}

    db_ok, db_detail = store.check_database()
    checks["database"] = {"ok": db_ok, "detail": db_detail}

    status = store.dataset_status()
    max_age = settings.dataset_max_age_hours
    fresh = bool(
        status["present"]
        and (max_age == 0 or (status["age_hours"] is not None and status["age_hours"] <= max_age))
    )
    checks["dataset"] = {
        "ok": fresh,
        "detail": (
            "missing" if not status["present"]
            else f"{status['age_hours']}h old"
            + ("" if max_age == 0 else f" (limit {max_age}h)")
        ),
        "sha256": status["sha256"],
        "modified_at": status["modified_at"],
    }

    if status["present"]:
        try:
            raw = store.load_dataset(allow_seed=False)
            periods = raw.get("meta", {}).get("periods") or []
            current = raw.get("meta", {}).get("current_period")
            contract_ok = bool(
                periods
                and current in periods
                and raw.get("journal")
                and raw.get("plan")
            )
            checks["dataset_contract"] = {
                "ok": contract_ok,
                "detail": f"current={current}, periods={len(periods)}",
            }
        except Exception as exc:  # noqa: BLE001 - readiness reports the failure
            checks["dataset_contract"] = {"ok": False, "detail": str(exc)}

    try:
        links = audit.verify_chain(store.load_signoffs(), {})
        chain_ok = all(link["status"] == "intact" for link in links)
        checks["audit_chain"] = {
            "ok": chain_ok,
            "detail": f"{len(links)} link(s), "
            + ("intact" if chain_ok else "BROKEN"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["audit_chain"] = {"ok": False, "detail": str(exc)}

    ok = all(check["ok"] for check in checks.values())
    payload = {
        "ok": ok,
        "environment": settings.app_env,
        "checks": checks,
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app.get("/api/meta")
def meta(principal: Principal = Depends(require("meta:read"))) -> dict:  # noqa: ARG001
    raw = dataset()
    return {
        "group": config.GROUP_NAME,
        "group_currency": config.GROUP_CURRENCY,
        "current_period": raw["meta"]["current_period"],
        "prior_period": raw["meta"]["prior_period"],
        "periods": raw["meta"]["periods"],
        "period_labels": config.PERIOD_LABELS,
        "source_systems": raw["meta"]["source_systems"],
        "entities": raw["entities"],
        "accounts": raw["accounts"],
        "drivers": config.DRIVERS,
        "non_discretionary": sorted(config.NON_DISCRETIONARY),
        "materiality": {
            "PL": {"amount_usd": str(config.MATERIALITY["PL"]["amount_usd"]), "min_pct": str(config.MATERIALITY["PL"]["min_pct"])},
            "BS": {"amount_usd": str(config.MATERIALITY["BS"]["amount_usd"]), "min_pct": str(config.MATERIALITY["BS"]["min_pct"])},
            "integrity_block_threshold_usd": str(config.INTEGRITY_BLOCK_THRESHOLD_USD),
        },
        "modes": MODE_LABELS,
        "stages": config.STAGE_LABELS,
        "versions": {
            "engine": config.ENGINE_VERSION,
            "verifier": config.VERIFIER_VERSION,
            "evidence_pack": config.EVIDENCE_PACK_VERSION,
        },
        "attestation_templates": config.ATTESTATION_TEMPLATES,
        "auth": {"mode": settings.auth_mode, "environment": settings.app_env},
    }


# ------------------------------------------------------------------ runs
@app.get("/api/runs")
def list_runs(principal: Principal = Depends(require("run:read"))) -> dict:  # noqa: ARG001
    return {"runs": store.list_runs()}


@app.post("/api/runs", status_code=201)
def create_run(
    body: CreateRun,
    principal: Principal = Depends(require("run:create")),
) -> dict:
    raw = dataset()
    period = body.period or raw["meta"]["current_period"]
    if period not in config.PERIODS:
        raise ApiError(
            422,
            "validation_error",
            f"period must be one of: {', '.join(config.PERIODS)}.",
        )
    run_id = store.new_run_id()
    payload = _execute(period, body.mode)
    status = _status(payload["verification"], signed=False)
    store.save_run(
        run_id,
        payload,
        status=status,
        period=payload["recon"]["period"],
        mode=body.mode,
        label=f"{config.PERIOD_LABELS.get(period, period)} close",
    )
    store.log_event(run_id, principal.display_name, "run.created", {
        "period": period,
        "mode": body.mode,
        "material_variances": payload["recon"]["stats"]["material_variances"],
        "status": status,
        "prepared_by": principal.display_name,
        "preparer_subject": principal.subject,
        "preparer_issuer": principal.issuer,
        "actor_subject": principal.subject,
        "actor_issuer": principal.issuer,
        "session_id": principal.session_id,
        "engine_version": config.ENGINE_VERSION,
        "verifier_version": config.VERIFIER_VERSION,
    })
    store.log_event(run_id, "close-engine", "reconcile.completed", payload["recon"]["stats"])
    store.log_event(run_id, "close-engine", "narrate.completed", {
        "sections": payload["commentary"]["stats"]["sections"],
        "figures": payload["commentary"]["stats"]["figures"],
        "mode": body.mode,
    })
    store.log_event(run_id, "close-engine", "verify.completed", {
        "status": payload["verification"]["status"],
        "blocking": payload["verification"]["gate"]["blocking"],
        "warnings": payload["verification"]["gate"]["warnings"],
        "figures_verified": payload["verification"]["stats"]["figures_checked"],
        "facts_rederived": payload["verification"]["stats"]["facts_checked"],
        "self_test": f"{payload['verification']['self_test']['detected']}/{payload['verification']['self_test']['total']}",
    })
    return _run_view(store.load_run(run_id), principal)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(require("run:read"))) -> dict:
    return _run_view(_load_run_or_404(run_id), principal)


@app.get("/api/runs/{run_id}/diff/{other_id}")
def compare_runs(
    run_id: str,
    other_id: str,
    principal: Principal = Depends(require("run:read")),  # noqa: ARG001
) -> dict:
    """What changed between two persisted runs of the close.

    `run_id` is the base (left) run and `other_id` the comparison (right) run.
    The response is a read model: no run, signature or event is modified.
    """
    base = _load_run_or_404(run_id)
    other = _load_run_or_404(other_id)
    base_signoffs = store.load_signoffs(run_id)
    other_signoffs = store.load_signoffs(other_id)
    _, links = _chain_state()
    base_link = next((link for link in links if link["run_id"] == run_id), None)
    return diff_runs(
        base,
        other,
        base_signoff=base_signoffs[-1] if base_signoffs else None,
        compare_signoff=other_signoffs[-1] if other_signoffs else None,
        base_link=base_link,
    )


@app.post("/api/runs/{run_id}/renarrate")
def renarrate(
    run_id: str,
    body: RenarrateRequest,
    principal: Principal = Depends(require("run:narrate")),
) -> dict:
    run = _load_run_or_404(run_id)
    if run["status"] == "signed":
        raise ApiError(409, "run_frozen", "A signed run cannot be re-narrated; open a new run.")
    raw = _dataset_for_period(run["period"])
    recon = run["payload"]["recon"]
    commentary = narrate(recon, mode=body.mode)
    verification = verify(raw, recon, commentary)
    payload = {**run["payload"], "commentary": commentary, "verification": verification}
    status = _status(verification, signed=False)
    store.save_run(run_id, payload, status=status, period=run["period"], mode=body.mode, label=run["label"])
    store.log_event(run_id, principal.display_name, "narrate.regenerated", {
        "mode": body.mode,
        "status": verification["status"],
        "blocking": verification["gate"]["blocking"],
        "drift": commentary["drift"],
        "actor_subject": principal.subject,
        "session_id": principal.session_id,
    })
    return _run_view(store.load_run(run_id), principal)


@app.post("/api/runs/{run_id}/verify")
def reverify(run_id: str, principal: Principal = Depends(require("run:verify"))) -> dict:
    run = _load_run_or_404(run_id)
    if run["status"] == "signed":
        raise ApiError(409, "run_frozen", "A signed run is frozen; open a new run to re-verify.")
    raw = _dataset_for_period(run["period"])
    verification = verify(raw, run["payload"]["recon"], run["payload"]["commentary"])
    payload = {**run["payload"], "verification": verification}
    status = _status(verification, signed=False)
    store.save_run(run_id, payload, status=status, period=run["period"], mode=run["mode"], label=run["label"])
    store.log_event(run_id, principal.display_name, "verify.rerun", {
        "status": verification["status"],
        "blocking": verification["gate"]["blocking"],
        "warnings": verification["gate"]["warnings"],
        "actor_subject": principal.subject,
        "session_id": principal.session_id,
    })
    return _run_view(store.load_run(run_id), principal)


@app.get("/api/runs/{run_id}/facts/{fact_id}")
def get_fact(
    run_id: str,
    fact_id: str,
    principal: Principal = Depends(require("fact:read")),  # noqa: ARG001
) -> dict:
    run = _load_run_or_404(run_id)
    for fact in run["payload"]["recon"]["facts"]:
        if fact["fact_id"] == fact_id:
            check = next(
                (c for c in run["payload"]["verification"]["fact_checks"] if c["fact_id"] == fact_id),
                None,
            )
            return {"fact": fact, "check": check}
    raise ApiError(404, "not_found", "Fact not found.")


# ---------------------------------------------------------- separation of duties
@app.post("/api/runs/{run_id}/signoff", status_code=201)
def sign_off(
    run_id: str,
    body: SignOffRequest,
    request: Request,
    principal: Principal = Depends(require("run:sign")),
) -> dict:
    # The lock closes the local SQLite read-then-write window. Phase 2 replaces
    # it with a serialisable Postgres transaction and unique constraints.
    with _SIGNOFF_LOCK:
        run = _load_run_or_404(run_id)
        if run["status"] == "signed":
            raise ApiError(409, "already_signed", "This run has already been signed off.")
        payload = run["payload"]
        governance = store.run_governance(run_id)
        if not governance["preparer_subject"]:
            raise ApiError(
                409,
                "preparer_not_attributed",
                "This run has no attributed preparer; open a new run before signing.",
            )
        approval = store.active_dual_control(run_id)
        same_person = principal.subject == governance["preparer_subject"]
        if same_person and approval is None:
            raise ApiError(
                409,
                "separation_of_duties",
                "Separation of duties: the preparer cannot sign this run without an "
                "admin-approved dual-control exception.",
            )
        if not same_person:
            approval = None  # an unused approval is not a waiver
        existing = store.load_signoffs()
        prev_hash = existing[-1]["chain_hash"] if existing else "genesis"
        try:
            record = audit.sign(
                run,
                payload["recon"],
                payload["commentary"],
                payload["verification"],
                signer=principal.display_name,
                role=principal.role_label(),
                note=body.note,
                attestations=[a.model_dump() for a in body.attestations],
                prev_chain_hash=prev_hash,
                sequence=store.next_sequence(),
                preparer=governance["preparer"],
                dual_control=approval is not None,
                signer_subject=principal.subject,
                signer_issuer=principal.issuer,
                preparer_subject=governance["preparer_subject"],
                preparer_issuer=governance["preparer_issuer"],
                dual_control_approval=approval,
                session_id=principal.session_id or "",
            )
        except audit.SignOffRefused as exc:
            raise ApiError(409, "signoff_refused", str(exc)) from exc

        store.save_signoff(record)
        store.save_run(
            run_id,
            payload,
            status="signed",
            period=run["period"],
            mode=run["mode"],
            label=run["label"],
        )
        store.log_event(run_id, principal.display_name, "signoff.recorded", {
            "signoff_id": record["signoff_id"],
            "chain_hash": record["chain_hash"],
            "attested": len(record["attestations"]),
            "preparer": record["preparer"],
            "dual_control": record["dual_control"],
            "dual_control_approval_id": (record.get("dual_control_approval") or {}).get("approval_id"),
            "signer_subject": principal.subject,
            "actor_subject": principal.subject,
            "session_id": principal.session_id,
        })
        logger.info(
            "signoff recorded run=%s signoff=%s subject=%s dual_control=%s request_id=%s",
            run_id, record["signoff_id"], principal.subject, record["dual_control"],
            request_id_for(request),
        )
    return _run_view(store.load_run(run_id), principal)


@app.get("/api/runs/{run_id}/dual-control")
def get_dual_control(
    run_id: str,
    principal: Principal = Depends(require("run:read")),  # noqa: ARG001
) -> dict:
    _load_run_or_404(run_id)
    return {
        "approval": _approval_summary(store.active_dual_control(run_id)),
        "history": [_approval_summary(a) for a in store.list_dual_control(run_id)],
    }


@app.post("/api/runs/{run_id}/dual-control", status_code=201)
def approve_dual_control(
    run_id: str,
    body: DualControlRequest,
    principal: Principal = Depends(require("policy:manage")),
) -> dict:
    # Serialised with sign-off so an approval cannot be created between the
    # signer's separation-of-duties check and the signature being written.
    with _SIGNOFF_LOCK:
        run = _load_run_or_404(run_id)
        if run["status"] == "signed":
            raise ApiError(409, "already_signed", "A signed run cannot receive a new exception.")
        governance = store.run_governance(run_id)
        if not governance["preparer_subject"]:
            raise ApiError(409, "preparer_not_attributed", "This run has no attributed preparer.")
        if principal.subject == governance["preparer_subject"]:
            raise ApiError(
                409,
                "approver_conflict",
                "The dual-control approver must be a different person from the preparer.",
            )
        if store.active_dual_control(run_id) is not None:
            raise ApiError(409, "approval_exists", "An active dual-control approval already exists.")

        now = datetime.now(timezone.utc)
        expires = body.expires_at or (now + timedelta(hours=24))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now + timedelta(minutes=5):
            raise ApiError(422, "validation_error", "expires_at must be at least five minutes in the future.")
        if expires > now + timedelta(days=7):
            raise ApiError(422, "validation_error", "expires_at may be at most seven days in the future.")

        approval = store.create_dual_control_approval(
            run_id=run_id,
            requested_by=governance["preparer"],
            requested_subject=governance["preparer_subject"],
            approved_by=principal.display_name,
            approved_subject=principal.subject,
            reason=body.reason.strip(),
            expires_at=expires.isoformat(timespec="seconds"),
        )
        store.log_event(run_id, principal.display_name, "dual_control.approved", {
            "approval_id": approval["approval_id"],
            "reason": approval["reason"],
            "expires_at": approval["expires_at"],
            "approved_subject": principal.subject,
            "requested_subject": governance["preparer_subject"],
            "actor_subject": principal.subject,
            "session_id": principal.session_id,
        })
    return {"approval": _approval_summary(approval)}


@app.delete("/api/runs/{run_id}/dual-control")
def revoke_dual_control(
    run_id: str,
    principal: Principal = Depends(require("policy:manage")),
) -> dict:
    with _SIGNOFF_LOCK:
        _load_run_or_404(run_id)
        approval = store.active_dual_control(run_id)
        if approval is None:
            raise ApiError(404, "not_found", "There is no active dual-control approval.")
        store.revoke_dual_control(approval["approval_id"], principal.display_name, principal.subject)
        store.log_event(run_id, principal.display_name, "dual_control.revoked", {
            "approval_id": approval["approval_id"],
            "actor_subject": principal.subject,
            "session_id": principal.session_id,
        })
    return {"ok": True, "approval_id": approval["approval_id"]}


# ------------------------------------------------------------------ evidence
@app.get("/api/runs/{run_id}/export")
def export_pack(
    run_id: str,
    principal: Principal = Depends(require("export:read")),
) -> Response:
    run = _load_run_or_404(run_id)
    payload = run["payload"]
    signoffs = store.load_signoffs(run["id"])
    signoff = signoffs[-1] if signoffs else None
    _, chain = _chain_state()
    pack = audit.evidence_pack(
        run,
        payload["recon"],
        payload["commentary"],
        payload["verification"],
        signoff,
        chain,
    )
    body = json.dumps(pack, indent=2)
    store.log_event(run["id"], principal.display_name, "audit.exported", {
        "format": "evidence_pack",
        "pack_id": pack.get("pack_id"),
        "pack_hash": pack.get("pack_hash"),
        "signoff_id": (signoff or {}).get("signoff_id"),
        "actor_subject": principal.subject,
        "session_id": principal.session_id,
    })
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="evidence-{run["id"]}.json"',
            "X-Pack-ID": str(pack.get("pack_id", "")),
            "X-Pack-Hash": str(pack.get("pack_hash", "")),
        },
    )


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(
    run_id: str,
    principal: Principal = Depends(require("export:read")),
) -> Response:
    run = _load_run_or_404(run_id)
    body = audit.commentary_csv(run["payload"]["recon"], run["payload"]["commentary"])
    store.log_event(run["id"], principal.display_name, "audit.exported", {
        "format": "commentary_csv",
        "actor_subject": principal.subject,
        "session_id": principal.session_id,
    })
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="commentary-{run["id"]}.csv"'},
    )


@app.get("/api/audit/chain")
def audit_chain(principal: Principal = Depends(require("chain:read"))) -> dict:  # noqa: ARG001
    signoffs, links = _chain_state()
    return {
        "signoffs": signoffs,
        "links": links,
        "intact": all(link["status"] == "intact" for link in links),
    }


@app.get("/api/dataset/meta")
def dataset_meta(principal: Principal = Depends(require("dataset:read"))) -> dict:  # noqa: ARG001
    raw = dataset()
    return {
        "meta": raw["meta"],
        "journal_lines": len(raw["journal"]),
        "plan_rows": len(raw["plan"]),
        "driver_rows": len(raw["drivers"]),
        "intercompany_pairs": len({r["pair_id"] for r in raw["intercompany"]}),
        "exceptions": raw["exceptions"],
    }


@app.get("/api/rollforward")
def rollforward_pivot(
    entity: str = "ALL",
    statement: str = "ALL",
    material_only: bool = False,
    principal: Principal = Depends(require("run:read")),  # noqa: ARG001
) -> dict:
    """Period × account pivot, July → October with the current period last."""
    if entity != "ALL" and entity not in config.ENTITY_BY_CODE:
        raise ApiError(422, "validation_error", "Unknown entity.")
    if statement not in {"ALL", "PL", "BS"}:
        raise ApiError(422, "validation_error", "statement must be ALL, PL or BS.")
    data = build_rollforward(dataset())
    rows = data["rows"]
    if entity != "ALL":
        rows = [row for row in rows if row["entity"] == entity]
    if statement != "ALL":
        rows = [row for row in rows if row["statement"] == statement]
    if material_only:
        rows = [row for row in rows if row["material_periods"]]
    return {
        **data,
        "rows": rows,
        "filter": {
            "entity": entity,
            "statement": statement,
            "material_only": material_only,
        },
        "stats": {**data["stats"], "filtered_rows": len(rows)},
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ------------------------------------------------------------------ errors
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):  # noqa: ANN001
    return error_response(
        request,
        exc.status_code,
        exc.code,
        exc.message,
        headers=exc.headers,
        details=exc.details,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):  # noqa: ANN001
    details = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        details.append({"field": location or "body", "message": str(error.get("msg", "invalid"))})
    message = details[0]["message"] if details else "The request is not valid."
    if details:
        message = f"{details[0]['field']}: {message}" if details[0]["field"] else message
    return error_response(request, 422, "validation_error", message, details=details)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):  # noqa: ANN001
    code = {
        400: "invalid_request",
        401: "authentication_required",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        429: "rate_limited",
    }.get(exc.status_code, "http_error")
    message = str(exc.detail) if exc.detail else "The request could not be completed."
    return error_response(request, exc.status_code, code, message)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):  # noqa: ANN001
    logger.exception(
        "unhandled error request_id=%s path=%s", request_id_for(request), request.url.path
    )
    return error_response(
        request,
        500,
        "internal_error",
        "An unexpected error occurred. Quote the request id when contacting support.",
    )
