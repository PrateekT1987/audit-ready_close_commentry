"""Identity, authorization, session and CSRF primitives.

Two modes are supported:

  * ``dev``  — local/test only. An operator picks one of the seeded dev
    identities or sends ``X-Dev-Subject`` headers. The mode is refused at
    startup outside local/test/dev.
  * ``oidc`` — corporate OpenID Connect (authorization code + PKCE) with a
    backend-for-frontend session. Roles are mapped server-side from IdP groups;
    the browser only receives an opaque session cookie.

Every figure of identity used for an audit record comes from a Principal, never
from a request body.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Iterable

from fastapi import Request
from starlette.responses import Response

from app import store
from app.security import ApiError
from app.settings import KNOWN_ROLES, settings

logger = logging.getLogger("ledgerline.auth")

CSRF_COOKIE = "ledgerline_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9:_.@|/-]{1,200}$")

# Permission model. Roles are additive; admin is the only wildcard.
PERMISSIONS: dict[str, frozenset[str]] = {
    "preparer": frozenset({
        "run:create", "run:read", "run:narrate", "run:verify",
        "fact:read", "meta:read", "dataset:read", "export:read", "chain:read",
    }),
    "reviewer": frozenset({
        "run:read", "fact:read", "meta:read", "dataset:read", "chain:read",
    }),
    "signer": frozenset({
        "run:read", "run:sign", "fact:read", "meta:read",
        "dataset:read", "export:read", "chain:read",
    }),
    "auditor": frozenset({
        "run:read", "fact:read", "meta:read", "dataset:read",
        "export:read", "chain:read",
    }),
    "admin": frozenset({"*"}),
}

ALL_PERMISSIONS = frozenset(
    permission for permissions in PERMISSIONS.values() for permission in permissions
) - {"*"}


@dataclass(frozen=True)
class Principal:
    """An authenticated person or service, derived server-side."""

    subject: str
    issuer: str
    display_name: str
    email: str
    roles: frozenset[str]
    auth_time: str
    session_id: str | None
    method: str  # oidc | dev-session | dev-header
    csrf_hash: str = ""

    @property
    def permissions(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted.update(PERMISSIONS.get(role, frozenset()))
        return frozenset(granted)

    def has(self, permission: str) -> bool:
        permissions = self.permissions
        return "*" in permissions or permission in permissions

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise ApiError(403, "forbidden", "Your role does not permit this action.")

    def role_label(self) -> str:
        if not self.roles:
            return "No role assigned"
        preferred = [r for r in ("signer", "preparer", "reviewer", "auditor", "admin") if r in self.roles]
        return ", ".join(preferred or sorted(self.roles))

    def view(self) -> dict:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "display_name": self.display_name,
            "email": self.email,
            "roles": sorted(self.roles),
            "role_label": self.role_label(),
            "permissions": sorted(self.permissions),
            "auth_time": self.auth_time,
            "method": self.method,
        }


# Seeded identities for local development and the UI switcher. Subjects are
# stable so a local sign-off chain is reproducible across restarts.
DEV_IDENTITIES: dict[str, dict] = {
    "preparer": {
        "subject": "dev:preparer",
        "display_name": "J. Okafor",
        "email": "j.okafor@example.invalid",
        "roles": ["preparer"],
    },
    "signer": {
        "subject": "dev:signer",
        "display_name": "A. Mensah",
        "email": "a.mensah@example.invalid",
        "roles": ["signer"],
    },
    "reviewer": {
        "subject": "dev:reviewer",
        "display_name": "L. Chen",
        "email": "l.chen@example.invalid",
        "roles": ["reviewer"],
    },
    "auditor": {
        "subject": "dev:auditor",
        "display_name": "M. Duarte",
        "email": "m.duarte@example.invalid",
        "roles": ["auditor"],
    },
    "controller": {
        "subject": "dev:controller",
        "display_name": "P. Raghavan",
        "email": "p.raghavan@example.invalid",
        "roles": ["preparer", "signer"],
    },
    "admin": {
        "subject": "dev:admin",
        "display_name": "S. Iyer",
        "email": "s.iyer@example.invalid",
        "roles": ["admin"],
    },
}
DEV_IDENTITY_ORDER = ("preparer", "signer", "reviewer", "auditor", "controller", "admin")


def dev_identity_catalogue() -> list[dict]:
    return [
        {
            "identity": key,
            "subject": DEV_IDENTITIES[key]["subject"],
            "display_name": DEV_IDENTITIES[key]["display_name"],
            "roles": DEV_IDENTITIES[key]["roles"],
        }
        for key in DEV_IDENTITY_ORDER
    ]


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _normalise_subject(value: str) -> str:
    subject = (value or "").strip()
    if not subject or not SUBJECT_PATTERN.match(subject):
        raise ApiError(401, "invalid_identity", "The identity subject is not valid.")
    return subject


def _normalise_roles(values: Iterable[str]) -> frozenset[str]:
    roles = {str(value).strip().lower() for value in values if str(value).strip()}
    return frozenset(roles & set(KNOWN_ROLES))


def roles_from_claims(claims: dict) -> frozenset[str]:
    """Map IdP group claims to local roles. Deny by default."""
    raw = claims.get(settings.oidc_groups_claim)
    if raw is None:
        raw = claims.get("groups", [])
    if isinstance(raw, str):
        groups = [raw]
    elif isinstance(raw, (list, tuple, set)):
        groups = [str(item) for item in raw]
    else:
        groups = []
    mapped = {settings.oidc_role_map.get(group, "") for group in groups}
    roles = frozenset(role for role in mapped if role in KNOWN_ROLES)
    if not roles and settings.oidc_default_roles:
        roles = frozenset(settings.oidc_default_roles)
    return roles


def principal_from_claims(claims: dict) -> Principal:
    subject = _normalise_subject(str(claims.get("sub", "")))
    issuer = str(claims.get("iss", "")).rstrip("/")
    if settings.oidc_issuer and issuer and issuer != settings.oidc_issuer:
        raise ApiError(401, "invalid_identity", "The identity issuer does not match the configured issuer.")
    email = str(claims.get("email", "") or claims.get("preferred_username", "") or "")
    display_name = str(
        claims.get("name", "")
        or claims.get("preferred_username", "")
        or email
        or subject
    )
    auth_time = str(claims.get("auth_time", "") or "")
    return Principal(
        subject=subject,
        issuer=issuer or settings.oidc_issuer,
        display_name=display_name[:160],
        email=email[:320],
        roles=roles_from_claims(claims),
        auth_time=auth_time,
        session_id=None,
        method="oidc",
    )


def create_session(principal: Principal) -> tuple[str, str, dict]:
    """Create a server-side session and return (cookie token, csrf token, row)."""
    token = new_token()
    csrf = new_token()
    row = store.create_session(
        session_id=hash_token(token),
        subject=principal.subject,
        issuer=principal.issuer,
        email=principal.email,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        auth_time=principal.auth_time,
        method=principal.method,
        csrf_hash=hash_token(csrf),
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    return token, csrf, row


def principal_from_session(row: dict) -> Principal:
    return Principal(
        subject=row["subject"],
        issuer=row["issuer"],
        display_name=row["display_name"],
        email=row["email"],
        roles=frozenset(row["roles"]),
        auth_time=row["auth_time"],
        session_id=row["id"],
        method=row["method"],
        csrf_hash=row["csrf_hash"],
    )


def _dev_header_principal(request: Request) -> Principal | None:
    if not (settings.auth_mode == "dev" and settings.dev_auth_headers):
        return None
    subject = request.headers.get("X-Dev-Subject", "").strip()
    if not subject:
        return None
    subject = _normalise_subject(subject)
    roles = _normalise_roles(request.headers.get("X-Dev-Roles", "").split(","))
    name = (request.headers.get("X-Dev-Name", "") or subject).strip()[:160]
    return Principal(
        subject=subject,
        issuer="dev",
        display_name=name,
        email=(request.headers.get("X-Dev-Email", "") or "").strip()[:320],
        roles=roles,
        auth_time=store.now_iso(),
        session_id=None,
        method="dev-header",
    )


def resolve_principal(request: Request) -> Principal | None:
    """Resolve and cache the caller's Principal for this request."""
    if getattr(request.state, "_principal_resolved", False):
        return getattr(request.state, "principal", None)

    principal: Principal | None = None
    header_principal = _dev_header_principal(request)
    if header_principal is not None:
        principal = header_principal
    else:
        token = request.cookies.get(settings.session_cookie, "")
        if token:
            row = store.load_session(hash_token(token))
            if row is not None:
                if store.session_needs_touch(row):
                    store.touch_session(row["id"], settings.session_idle_seconds)
                principal = principal_from_session(row)

    request.state.principal = principal
    request.state._principal_resolved = True
    return principal


def require(permission: str):
    """FastAPI dependency: authenticated and holding ``permission``."""

    def dependency(request: Request) -> Principal:
        principal = resolve_principal(request)
        if principal is None:
            raise ApiError(401, "authentication_required", "Sign in to continue.")
        principal.require(permission)
        return principal

    return dependency


def detached_principal(identity: str) -> Principal:
    """Build (but do not persist) a principal for a dev identity."""
    data = DEV_IDENTITIES.get(identity)
    if data is None:
        raise ApiError(422, "unknown_identity", "Unknown development identity.")
    return Principal(
        subject=data["subject"],
        issuer="dev",
        display_name=data["display_name"],
        email=data["email"],
        roles=frozenset(data["roles"]),
        auth_time=store.now_iso(),
        session_id=None,
        method="dev-session",
    )


def csrf_cookie_value(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE, "")


def validate_csrf(request: Request, principal: Principal) -> None:
    """Double-submit + origin check for state-changing requests.

    The session cookie is HttpOnly; the CSRF cookie is readable by the app and
    must be echoed in the ``X-CSRF-Token`` header. Both are compared against the
    server-side session hash, so a copied cookie alone is not enough.
    """
    if principal.method == "dev-header":
        # Machine-to-machine local testing only; production rejects this mode.
        return
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied or len(supplied) > 256:
        raise ApiError(403, "csrf_failed", "A valid CSRF token is required.")
    if not hmac.compare_digest(hash_token(supplied), principal.csrf_hash):
        raise ApiError(403, "csrf_failed", "The CSRF token is invalid or expired.")

    origin = request.headers.get("origin")
    if origin:
        allowed = set(settings.cors_origins)
        if settings.public_origin:
            allowed.add(settings.public_origin)
        allowed.add(str(request.base_url).rstrip("/"))
        if origin.rstrip("/") not in allowed:
            raise ApiError(403, "csrf_failed", "The request origin is not allowed.")


def set_session_cookies(response: Response, token: str, csrf: str) -> None:
    max_age = settings.session_idle_seconds
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.session_cookie, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def auth_state(principal: Principal | None) -> dict:
    state = {
        "authenticated": principal is not None,
        "auth_mode": settings.auth_mode,
        "app_env": settings.app_env,
        "permissions": sorted(principal.permissions) if principal else [],
    }
    if principal:
        state["principal"] = principal.view()
        state["csrf_token"] = None  # filled from the readable cookie by the route
    elif settings.auth_mode == "dev":
        state["dev_identities"] = dev_identity_catalogue()
    return state


def viewer_context(
    principal: Principal,
    *,
    preparer_subject: str,
    signed: bool,
    approval: dict | None,
) -> dict:
    same_as_preparer = bool(preparer_subject) and principal.subject == preparer_subject
    attributed = bool(preparer_subject)
    can_sign = (
        principal.has("run:sign")
        and not signed
        and attributed
        and (not same_as_preparer or approval is not None)
    )
    return {
        **principal.view(),
        "same_as_preparer": same_as_preparer,
        "preparer_attributed": attributed,
        "can_sign": can_sign,
        "can_approve_dual_control": principal.has("policy:manage") and not signed,
        "can_create_run": principal.has("run:create"),
        "can_export": principal.has("export:read"),
        "can_renarrate": principal.has("run:narrate") and not signed,
        "can_verify": principal.has("run:verify") and not signed,
    }
