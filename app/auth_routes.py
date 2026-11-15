"""Authentication routes: corporate OIDC, dev sessions and session state."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app import auth, store
from app.security import ApiError, apply_security_headers
from app.settings import settings

logger = logging.getLogger("ledgerline.auth_routes")
router = APIRouter()

_oauth = None


def configure_oidc() -> None:
    """Register the corporate OIDC client once, if OIDC is selected."""
    global _oauth
    if not settings.oidc_enabled or _oauth is not None:
        return
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="corporate",
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={
            "scope": settings.oidc_scopes,
            "code_challenge_method": "S256",
        },
    )
    _oauth = oauth
    logger.info("OIDC client registered for issuer %s", settings.oidc_issuer)


def oauth_client():
    if _oauth is None:
        raise ApiError(503, "oidc_unavailable", "Corporate sign-in is not configured.")
    return _oauth


class DevLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str = Field(min_length=1, max_length=40)


@router.get("/api/auth/me")
def me(request: Request) -> dict:
    principal = auth.resolve_principal(request)
    state = auth.auth_state(principal)
    if principal is not None:
        csrf = auth.csrf_cookie_value(request)
        state["csrf_token"] = csrf or None
        state["principal"] = principal.view()
    return state


@router.post("/auth/dev/login")
def dev_login(request: Request, body: DevLoginRequest):
    if settings.auth_mode != "dev":
        raise ApiError(404, "not_found", "Not found.")
    principal = auth.detached_principal(body.identity)
    token, csrf, row = auth.create_session(principal)
    session_principal = auth.principal_from_session(row)
    state = auth.auth_state(session_principal)
    state["csrf_token"] = csrf
    state["principal"] = session_principal.view()
    response = JSONResponse(state)
    auth.set_session_cookies(response, token, csrf)
    apply_security_headers(response, request)
    logger.info(
        "dev session created subject=%s roles=%s request_id=%s",
        principal.subject, sorted(principal.roles), getattr(request.state, "request_id", ""),
    )
    return response


@router.post("/auth/logout")
def logout(request: Request):
    principal = auth.resolve_principal(request)
    if principal is not None and principal.session_id:
        store.revoke_session(principal.session_id, "logout")
    response = JSONResponse({"ok": True})
    auth.clear_session_cookies(response)
    apply_security_headers(response, request)
    return response


@router.get("/auth/login")
async def oidc_login(request: Request):
    if not settings.oidc_enabled:
        raise ApiError(404, "not_found", "Corporate sign-in is not enabled.")
    client = oauth_client()
    return await client.corporate.authorize_redirect(request, settings.oidc_redirect_uri)


@router.get("/auth/callback")
async def oidc_callback(request: Request):
    if not settings.oidc_enabled:
        raise ApiError(404, "not_found", "Corporate sign-in is not enabled.")
    client = oauth_client()
    try:
        token = await client.corporate.authorize_access_token(request)
        claims = token.get("userinfo")
        if not claims:
            claims = await client.corporate.userinfo(token=token)
        principal = auth.principal_from_claims(dict(claims))
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - never leak provider detail to the browser
        logger.exception(
            "OIDC callback failed request_id=%s", getattr(request.state, "request_id", "")
        )
        response = RedirectResponse("/?auth_error=oidc_failed", status_code=303)
        apply_security_headers(response, request)
        return response

    token_value, csrf, row = auth.create_session(principal)
    logger.info(
        "oidc session created subject=%s roles=%s request_id=%s",
        principal.subject, sorted(principal.roles), getattr(request.state, "request_id", ""),
    )
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookies(response, token_value, csrf)
    apply_security_headers(response, request)
    return response
