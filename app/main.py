"""FastAPI application: the close pipeline as a service.

Pipeline:  reconcile -> narrate -> verify -> (human) sign-off
Every stage is exposed so the workpaper can be inspected, re-run and audited.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, store
from app.engine import audit
from app.engine.narrate import MODE_LABELS, narrate
from app.engine.reconcile import reconcile
from app.engine.verify import verify

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Ledgerline — Audit-Ready Close Commentary", version="1.0.0")
app.add_middleware(GZipMiddleware, minimum_size=800)

_dataset_cache: dict | None = None


def dataset() -> dict:
    global _dataset_cache
    if _dataset_cache is None:
        _dataset_cache = store.load_dataset()
    return _dataset_cache


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


def _run_view(run: dict) -> dict:
    payload = run["payload"]
    recon = payload["recon"]
    commentary = payload["commentary"]
    verification = payload["verification"]
    signoffs = store.load_signoffs(run["id"])
    signoff = signoffs[-1] if signoffs else None
    governance = store.run_governance(run["id"])
    state = audit.preflight(recon, verification, [])
    _, chain = _chain_state()

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
            "dual_control": governance["dual_control"],
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


def _dataset_for_period(period: str) -> dict:
    raw = dataset()
    if period and period != raw["meta"]["current_period"]:
        raw = {**raw, "meta": {**raw["meta"], "current_period": period}}
    return raw


class CreateRun(BaseModel):
    period: str | None = None
    mode: str = Field(default="strict", pattern="^(strict|drift)$")
    prepared_by: str = Field(default="close-engine", min_length=1)
    dual_control: bool = False


class RenarrateRequest(BaseModel):
    mode: str = Field(default="strict", pattern="^(strict|drift)$")


class Attestation(BaseModel):
    finding_id: str
    rationale: str


class SignOffRequest(BaseModel):
    signer: str
    role: str = "Group Controller"
    note: str = ""
    attestations: list[Attestation] = Field(default_factory=list)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "ledgerline", "version": app.version}


@app.get("/api/meta")
def meta() -> dict:
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
    }


@app.get("/api/runs")
def list_runs() -> dict:
    return {"runs": store.list_runs()}


@app.post("/api/runs", status_code=201)
def create_run(body: CreateRun) -> dict:
    raw = dataset()
    period = body.period or raw["meta"]["current_period"]
    preparer = body.prepared_by.strip()
    if not preparer:
        raise HTTPException(422, "prepared_by must name the preparer of record")
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
    store.log_event(run_id, preparer, "run.created", {
        "period": period,
        "mode": body.mode,
        "material_variances": payload["recon"]["stats"]["material_variances"],
        "status": status,
        "prepared_by": preparer,
        "dual_control": body.dual_control,
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
    run = store.load_run(run_id)
    return _run_view(run)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return _run_view(run)


@app.post("/api/runs/{run_id}/renarrate")
def renarrate(run_id: str, body: RenarrateRequest) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run["status"] == "signed":
        raise HTTPException(409, "a signed run cannot be re-narrated; open a new run")
    raw = _dataset_for_period(run["period"])
    recon = run["payload"]["recon"]
    commentary = narrate(recon, mode=body.mode)
    verification = verify(raw, recon, commentary)
    payload = {**run["payload"], "commentary": commentary, "verification": verification}
    status = _status(verification, signed=False)
    store.save_run(run_id, payload, status=status, period=run["period"], mode=body.mode, label=run["label"])
    store.log_event(run_id, "close-engine" if body.mode == "strict" else "adversarial-test", "narrate.regenerated", {
        "mode": body.mode,
        "status": verification["status"],
        "blocking": verification["gate"]["blocking"],
        "drift": commentary["drift"],
    })
    return _run_view(store.load_run(run_id))


@app.post("/api/runs/{run_id}/verify")
def reverify(run_id: str) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run["status"] == "signed":
        raise HTTPException(409, "a signed run is frozen; open a new run to re-verify")
    raw = _dataset_for_period(run["period"])
    verification = verify(raw, run["payload"]["recon"], run["payload"]["commentary"])
    payload = {**run["payload"], "verification": verification}
    status = _status(verification, signed=False)
    store.save_run(run_id, payload, status=status, period=run["period"], mode=run["mode"], label=run["label"])
    store.log_event(run_id, "auditor", "verify.rerun", {
        "status": verification["status"],
        "blocking": verification["gate"]["blocking"],
        "warnings": verification["gate"]["warnings"],
    })
    return _run_view(store.load_run(run_id))


@app.get("/api/runs/{run_id}/facts/{fact_id}")
def get_fact(run_id: str, fact_id: str) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    for fact in run["payload"]["recon"]["facts"]:
        if fact["fact_id"] == fact_id:
            check = next(
                (c for c in run["payload"]["verification"]["fact_checks"] if c["fact_id"] == fact_id),
                None,
            )
            return {"fact": fact, "check": check}
    raise HTTPException(404, "fact not found")


@app.post("/api/runs/{run_id}/signoff", status_code=201)
def sign_off(run_id: str, body: SignOffRequest) -> dict:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run["status"] == "signed":
        raise HTTPException(409, "this run has already been signed off")
    payload = run["payload"]
    governance = store.run_governance(run_id)
    existing = store.load_signoffs()
    prev_hash = existing[-1]["chain_hash"] if existing else "genesis"
    try:
        record = audit.sign(
            run,
            payload["recon"],
            payload["commentary"],
            payload["verification"],
            signer=body.signer,
            role=body.role,
            note=body.note,
            attestations=[a.model_dump() for a in body.attestations],
            prev_chain_hash=prev_hash,
            sequence=store.next_sequence(),
            preparer=governance["preparer"],
            dual_control=governance["dual_control"],
        )
    except audit.SignOffRefused as exc:
        raise HTTPException(409, str(exc)) from exc

    store.save_signoff(record)
    store.save_run(
        run_id,
        payload,
        status="signed",
        period=run["period"],
        mode=run["mode"],
        label=run["label"],
    )
    store.log_event(run_id, record["signer"], "signoff.recorded", {
        "signoff_id": record["signoff_id"],
        "chain_hash": record["chain_hash"],
        "attested": len(record["attestations"]),
        "preparer": record["preparer"],
        "dual_control": record["dual_control"],
    })
    return _run_view(store.load_run(run_id))


@app.get("/api/runs/{run_id}/export")
def export_pack(run_id: str) -> Response:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
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
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="evidence-{run["id"]}.json"'},
    )


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: str) -> Response:
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    body = audit.commentary_csv(run["payload"]["recon"], run["payload"]["commentary"])
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="commentary-{run["id"]}.csv"'},
    )


@app.get("/api/audit/chain")
def audit_chain() -> dict:
    signoffs, links = _chain_state()
    return {
        "signoffs": signoffs,
        "links": links,
        "intact": all(link["status"] == "intact" for link in links),
    }


@app.get("/api/dataset/meta")
def dataset_meta() -> dict:
    raw = dataset()
    return {
        "meta": raw["meta"],
        "journal_lines": len(raw["journal"]),
        "plan_rows": len(raw["plan"]),
        "driver_rows": len(raw["drivers"]),
        "intercompany_pairs": len({r["pair_id"] for r in raw["intercompany"]}),
        "exceptions": raw["exceptions"],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
def http_error(request, exc):  # noqa: ANN001, ARG001
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
