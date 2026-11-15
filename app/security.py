"""HTTP security middleware: request ids, limits, CSRF, headers and errors.

The middleware is deliberately boring: every response carries a request id and
the security headers, invalid state-changing requests are refused before they
reach a route, and every error uses one envelope so the UI and operators can
quote the same identifier.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from fnmatch import fnmatch

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.settings import settings

logger = logging.getLogger("ledgerline.security")

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ApiError(Exception):
    """A deliberate, client-safe API error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        details: list[dict] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.details = details or []
        super().__init__(message)


def request_id_for(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    if incoming and REQUEST_ID_PATTERN.match(incoming):
        return incoming
    generated = uuid.uuid4().hex
    request.state.request_id = generated
    return generated


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    details: list[dict] | None = None,
) -> JSONResponse:
    payload: dict = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_for(request),
        }
    }
    if details:
        payload["error"]["details"] = details
    response = JSONResponse(payload, status_code=status_code, headers=headers or {})
    apply_security_headers(response, request)
    return response


def apply_security_headers(response: Response, request: Request) -> None:
    response.headers.setdefault("X-Request-ID", request_id_for(request))
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    connect = ["'self'", *settings.cors_origins]
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        f"connect-src {' '.join(connect)}; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    if settings.secure_cookies or request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    path = request.url.path
    if path.startswith("/api/") or path == "/":
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=300")


class RateLimiter:
    """In-process sliding-window limiter.

    This is a backstop, not the edge control: behind multiple replicas the WAF
    or ingress quota is authoritative. Limits are per principal (or per client
    IP before authentication) and per bucket.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int = 60) -> int | None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry = max(1, int(bucket[0] + window_seconds - now) + 1)
                return retry
            bucket.append(now)
            if len(self._hits) > 10_000:
                for stale in [k for k, v in self._hits.items() if not v]:
                    self._hits.pop(stale, None)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


CSRF_EXEMPT = {
    "/auth/login",
    "/auth/callback",
    "/auth/dev/login",
    "/api/auth/me",
}
PUBLIC_PATHS = {
    "/api/health",
    "/api/ready",
    "/api/auth/me",
    "/",
}
LOCAL_ASSET_PREFIXES = ("/static/", "/favicon")


def _client_key(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"subject:{principal.subject}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _bucket_for(path: str, method: str) -> tuple[str, int]:
    if path.startswith("/auth/"):
        return "auth", settings.rate_limit_auth_per_minute
    if path.endswith("/export") or path.endswith("/export.csv"):
        return "export", settings.rate_limit_export_per_minute
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read", settings.rate_limit_read_per_minute
    return "write", settings.rate_limit_write_per_minute


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    for pattern in settings.allowed_hosts:
        pattern = pattern.lower()
        if pattern == host or fnmatch(host, pattern):
            return True
    return False


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        request_id_for(request)
        path = request.url.path
        method = request.method.upper()

        if not _host_allowed(request.url.hostname or ""):
            return error_response(
                request, 400, "invalid_host", "The request host is not allowed."
            )

        if not (path.startswith("/api") or path.startswith("/auth")):
            response = await call_next(request)
            apply_security_headers(response, request)
            return response

        # Resolve identity once; routes and CSRF validation reuse it.
        from app.auth import resolve_principal, validate_csrf

        principal = resolve_principal(request)

        if method not in {"GET", "HEAD", "OPTIONS"}:
            declared = request.headers.get("content-length")
            if declared:
                try:
                    length = int(declared)
                except ValueError:
                    return error_response(
                        request, 400, "invalid_request", "The Content-Length header is invalid."
                    )
                if length > settings.max_body_bytes:
                    return error_response(
                        request,
                        413,
                        "payload_too_large",
                        f"The request body exceeds {settings.max_body_bytes} bytes.",
                    )
            if path not in CSRF_EXEMPT:
                try:
                    if principal is not None:
                        validate_csrf(request, principal)
                except ApiError as exc:
                    return error_response(
                        request, exc.status_code, exc.code, exc.message, headers=exc.headers
                    )

        if path not in PUBLIC_PATHS and method != "OPTIONS" and not path.startswith(LOCAL_ASSET_PREFIXES):
            bucket, limit = _bucket_for(path, method)
            retry_after = limiter.hit(f"{_client_key(request)}:{bucket}", limit)
            if retry_after is not None:
                return error_response(
                    request,
                    429,
                    "rate_limited",
                    "Too many requests. Try again shortly.",
                    headers={"Retry-After": str(retry_after)},
                )

        response = await call_next(request)
        apply_security_headers(response, request)
        return response
