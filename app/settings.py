"""Environment-backed runtime settings for the security perimeter.

The defaults are safe for local development only. Staging and production are
fail-closed: a missing secret, a trusted-host wildcard, development
authentication or insecure cookies stops the process from starting rather than
silently exposing financial data.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field, replace
from typing import Mapping

logger = logging.getLogger("ledgerline.settings")

APP_ENVS = {"local", "test", "dev", "staging", "production"}
AUTH_MODES = {"dev", "oidc"}
PROTECTED_ENVS = {"staging", "production"}
KNOWN_ROLES = ("preparer", "reviewer", "signer", "auditor", "admin")


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot be used safely."""


def _env(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = environ.get(name)
    return default if value is None or value == "" else value


def _bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _env(environ, name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {raw!r}")


def _int(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = _env(environ, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return value


def _list(environ: Mapping[str, str], name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _env(environ, name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _roles(environ: Mapping[str, str], name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    roles = _list(environ, name, default)
    unknown = sorted(set(roles) - set(KNOWN_ROLES))
    if unknown:
        raise ConfigurationError(f"{name} contains unknown roles: {', '.join(unknown)}")
    return roles


@dataclass(frozen=True)
class Settings:
    app_env: str
    auth_mode: str
    session_secret: str
    session_cookie: str
    session_idle_seconds: int
    session_absolute_seconds: int
    secure_cookies: bool
    allowed_hosts: tuple[str, ...]
    cors_origins: tuple[str, ...]
    public_origin: str
    max_body_bytes: int
    enable_docs: bool
    allow_dataset_seed: bool
    dataset_max_age_hours: int
    dev_auth_headers: bool
    rate_limit_read_per_minute: int
    rate_limit_write_per_minute: int
    rate_limit_export_per_minute: int
    rate_limit_auth_per_minute: int
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_groups_claim: str = "groups"
    oidc_role_map: dict[str, str] = field(default_factory=dict)
    oidc_default_roles: tuple[str, ...] = ()

    @property
    def is_protected(self) -> bool:
        return self.app_env in PROTECTED_ENVS

    @property
    def oidc_enabled(self) -> bool:
        return self.auth_mode == "oidc"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ

        app_env = (_env(env, "APP_ENV", "local") or "local").strip().lower()
        if app_env not in APP_ENVS:
            raise ConfigurationError(f"APP_ENV must be one of {sorted(APP_ENVS)}")

        protected = app_env in PROTECTED_ENVS
        auth_mode = (_env(env, "AUTH_MODE", "oidc" if protected else "dev") or "dev").strip().lower()
        if auth_mode not in AUTH_MODES:
            raise ConfigurationError(f"AUTH_MODE must be one of {sorted(AUTH_MODES)}")
        if protected and auth_mode != "oidc":
            raise ConfigurationError(
                "AUTH_MODE=dev is local-only; staging and production must use OIDC."
            )

        session_secret = _env(env, "SESSION_SECRET", "") or ""
        if not session_secret:
            if protected:
                raise ConfigurationError("SESSION_SECRET must be set in staging and production.")
            session_secret = secrets.token_urlsafe(48)
            logger.warning("SESSION_SECRET unset: generated an ephemeral development secret.")
        if protected and len(session_secret) < 32:
            raise ConfigurationError("SESSION_SECRET must be at least 32 characters.")

        explicit_hosts = "ALLOWED_HOSTS" in env
        allowed_hosts = _list(
            env, "ALLOWED_HOSTS", ("127.0.0.1", "localhost", "testserver")
        )
        if protected and not explicit_hosts:
            raise ConfigurationError("ALLOWED_HOSTS must be set explicitly in staging and production.")
        if protected and "*" in allowed_hosts:
            raise ConfigurationError("ALLOWED_HOSTS cannot contain a wildcard in staging and production.")
        if not allowed_hosts:
            raise ConfigurationError("ALLOWED_HOSTS cannot be empty.")

        public_origin = (_env(env, "PUBLIC_ORIGIN", "") or "").rstrip("/")
        if protected and not public_origin:
            raise ConfigurationError("PUBLIC_ORIGIN must be set in staging and production.")
        if public_origin and not public_origin.startswith("https://") and protected:
            raise ConfigurationError("PUBLIC_ORIGIN must use https in staging and production.")

        secure_cookies = _bool(env, "SECURE_COOKIES", protected)
        if protected and not secure_cookies:
            raise ConfigurationError("SECURE_COOKIES cannot be disabled in staging and production.")

        dev_auth_headers = _bool(env, "DEV_AUTH_HEADERS", not protected)
        if protected and dev_auth_headers:
            raise ConfigurationError("DEV_AUTH_HEADERS cannot be enabled in staging and production.")

        idle_minutes = _int(env, "SESSION_IDLE_MINUTES", 480, minimum=5)
        absolute_hours = _int(env, "SESSION_ABSOLUTE_HOURS", 12, minimum=1)

        role_map_raw = _env(env, "OIDC_ROLE_MAP", "{}") or "{}"
        try:
            role_map = json.loads(role_map_raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("OIDC_ROLE_MAP must be a JSON object of group -> role.") from exc
        if not isinstance(role_map, dict):
            raise ConfigurationError("OIDC_ROLE_MAP must be a JSON object.")
        unknown_roles = sorted({str(v) for v in role_map.values()} - set(KNOWN_ROLES))
        if unknown_roles:
            raise ConfigurationError(
                f"OIDC_ROLE_MAP maps to unknown roles: {', '.join(unknown_roles)}"
            )

        settings = cls(
            app_env=app_env,
            auth_mode=auth_mode,
            session_secret=session_secret,
            session_cookie=(_env(env, "SESSION_COOKIE_NAME", "ledgerline_session") or "ledgerline_session"),
            session_idle_seconds=idle_minutes * 60,
            session_absolute_seconds=absolute_hours * 3600,
            secure_cookies=secure_cookies,
            allowed_hosts=allowed_hosts,
            cors_origins=_list(env, "CORS_ORIGINS", ()),
            public_origin=public_origin,
            max_body_bytes=_int(env, "MAX_BODY_BYTES", 1_048_576, minimum=1024),
            enable_docs=_bool(env, "ENABLE_DOCS", not protected),
            allow_dataset_seed=_bool(env, "ALLOW_DATASET_SEED", not protected),
            dataset_max_age_hours=_int(
                env, "DATASET_MAX_AGE_HOURS", 36 if protected else 0, minimum=0
            ),
            dev_auth_headers=dev_auth_headers,
            rate_limit_read_per_minute=_int(env, "RATE_LIMIT_READ_PER_MINUTE", 600, minimum=1),
            rate_limit_write_per_minute=_int(env, "RATE_LIMIT_WRITE_PER_MINUTE", 120, minimum=1),
            rate_limit_export_per_minute=_int(env, "RATE_LIMIT_EXPORT_PER_MINUTE", 30, minimum=1),
            rate_limit_auth_per_minute=_int(env, "RATE_LIMIT_AUTH_PER_MINUTE", 30, minimum=1),
            oidc_issuer=(_env(env, "OIDC_ISSUER", "") or "").rstrip("/"),
            oidc_client_id=_env(env, "OIDC_CLIENT_ID", "") or "",
            oidc_client_secret=_env(env, "OIDC_CLIENT_SECRET", "") or "",
            oidc_redirect_uri=_env(env, "OIDC_REDIRECT_URI", "") or "",
            oidc_scopes=_env(env, "OIDC_SCOPES", "openid profile email") or "openid profile email",
            oidc_groups_claim=_env(env, "OIDC_GROUPS_CLAIM", "groups") or "groups",
            oidc_role_map={str(k): str(v) for k, v in role_map.items()},
            oidc_default_roles=_roles(env, "OIDC_DEFAULT_ROLES", ()),
        )

        if settings.oidc_enabled:
            missing = [
                name
                for name, value in (
                    ("OIDC_ISSUER", settings.oidc_issuer),
                    ("OIDC_CLIENT_ID", settings.oidc_client_id),
                    ("OIDC_CLIENT_SECRET", settings.oidc_client_secret),
                    ("OIDC_REDIRECT_URI", settings.oidc_redirect_uri),
                )
                if not value
            ]
            if missing:
                raise ConfigurationError(
                    "OIDC is selected but required settings are missing: " + ", ".join(missing)
                )
            if protected and not settings.oidc_issuer.startswith("https://"):
                raise ConfigurationError("OIDC_ISSUER must use https in staging and production.")
            if protected and not settings.oidc_redirect_uri.startswith("https://"):
                raise ConfigurationError("OIDC_REDIRECT_URI must use https in staging and production.")

        return settings

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes)


settings = Settings.from_env()
