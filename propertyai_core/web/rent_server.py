"""Loopback HTTP bridge for the integrated W1 Rent API and authenticated shell."""
from __future__ import annotations

from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Type
from urllib.parse import urlsplit
from uuid import uuid4

from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.empty_shell import EmptyShell
from propertyai_core.web.rent_api import RentAPI, RentSessionPort, _cookie_token
from propertyai_core.web.rent_longstay_admin import LongstayAdminContext, RentLongstayAdmin

if TYPE_CHECKING:
    from propertyai_core.web.rent_operations import RentOperationsUI

OPERATOR_HTML = Path(__file__).with_name("operator.html")
_SENSITIVE_HEADERS = frozenset({
    "cookie", "authorization", "x-csrf-token", "x-organization-id", "x-actor-party-id",
    "x-subject", "x-capabilities", "x-request-id", "idempotency-key", "content-type",
})
_I2_ADMIN_CONTRACT = re.compile(r"^/api/v1/longstay/admin/contracts/[^/]+$")
_I2_CONTRACT_REVISION = re.compile(r"^/api/v1/longstay/contracts/[^/]+/revisions$")
_I2_ACCOUNT_REVISION = re.compile(r"^/api/v1/money-accounts/[^/]+/revisions$")
_I2_SHARED_NAV = (
    '<nav data-i2-shared-navigation aria-label="공통 월세 업무 탐색" '
    'style="display:flex;gap:.5rem;flex-wrap:wrap;padding:.65rem 1rem;border-bottom:1px solid #d9ddd7;background:#fff">'
    '<a href="/app">홈</a>'
    '<a href="/app/rent/contracts">계약·거주</a>'
    '<a href="/app/rent/master-data">기준정보</a>'
    '<a href="/rent">월세 운영</a>'
    '</nav>'
)
_I2_SHELL_LINKS = {
    'href="#rent"': 'href="/rent"',
    'href="#contracts"': 'href="/app/rent/contracts"',
    'href="#funding"': 'href="/rent"',
    'href="#settings"': 'href="/app/rent/master-data"',
}


def _safe_headers(message) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    items = message.raw_items() if hasattr(message, "raw_items") else message.items()
    for key, value in items:
        name = key.lower()
        if name in _SENSITIVE_HEADERS and name in seen:
            raise rent_error("UNAUTHENTICATED")
        seen.add(name)
        result[key] = value
    return result


def _normalized_url(raw_url: str) -> tuple[str, str]:
    parsed = urlsplit(raw_url)
    path = parsed.path.rstrip("/") or "/"
    normalized = path + (("?" + parsed.query) if parsed.query else "")
    return path, normalized


def _i2_admin_capability(method: str, path: str) -> str | None:
    if method in {"GET", "HEAD"} and path in {"/app/rent/contracts", "/app/rent/master-data"}:
        return "READ"
    if method == "GET" and path == "/api/v1/longstay/admin/reference-data":
        return "WRITE"
    if method == "GET" and _I2_ADMIN_CONTRACT.fullmatch(path):
        return "READ"
    if method == "POST" and (
        _I2_CONTRACT_REVISION.fullmatch(path) or _I2_ACCOUNT_REVISION.fullmatch(path)
    ):
        return "WRITE"
    return None


def _compose_i2_html(content: bytes, *, csrf_token: str | None = None) -> bytes:
    """Compose shared navigation/bootstrap without mutating frozen W2 template blobs."""
    if not content:
        return content
    try:
        page = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    if csrf_token is not None:
        bootstrap = "<script>window.RENT_CSRF_TOKEN=" + json.dumps(csrf_token) + ";</script>"
        if "</head>" in page:
            page = page.replace("</head>", bootstrap + "</head>", 1)
    if "<body>" in page:
        page = page.replace("<body>", "<body>" + _I2_SHARED_NAV, 1)
    return page.encode("utf-8")


def _compose_i2_shell(content: bytes) -> bytes:
    if not content:
        return content
    try:
        page = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    for old, new in _I2_SHELL_LINKS.items():
        page = page.replace(old, new)
    return page.encode("utf-8")


def handler_class(
    api: RentAPI,
    sessions: RentSessionPort | None = None,
    shell: EmptyShell | None = None,
    *,
    longstay_admin: RentLongstayAdmin | None = None,
    operations: RentOperationsUI | None = None,
) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # TEST harnesses retain their own explicit evidence.
            return

        def _send(self, status: int, content: bytes, mime: str, extra: Mapping[str, str] | None = None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            for key, value in (extra or {}).items():
                if key.lower() in {"content-type", "content-length", "cache-control"}:
                    continue
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _error(self, exc: RentError):
            request_id = str(uuid4())
            body = json.dumps(exc.wire(request_id), separators=(",", ":")).encode()
            self._send(exc.status, body, "application/json", {"X-Request-ID": request_id})

        def _handle(self, method: str):
            try:
                headers = _safe_headers(self.headers)
                path_only, normalized_url = _normalized_url(self.path)

                if shell is not None and (
                    (method in {"GET", "HEAD"} and path_only == "/app")
                    or (method == "POST" and path_only == "/auth/logout")
                ):
                    result = shell.handle(method, normalized_url, headers)
                    mime = result.headers.get("Content-Type", "application/octet-stream")
                    body = _compose_i2_shell(result.body) if mime.startswith("text/html") else result.body
                    self._send(result.status, body, mime, result.headers)
                    return

                admin_required = _i2_admin_capability(method, path_only) if longstay_admin is not None else None
                if admin_required is not None:
                    service, _capabilities, context = api._scope(method, headers, admin_required)
                    if context is None:
                        raise rent_error("NOT_AUTHORIZED")
                    raw_body = (
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                        if method in {"POST", "PUT", "PATCH", "DELETE"} else b""
                    )
                    result = longstay_admin.handle(
                        LongstayAdminContext(service, context.csrf_token, context.principal),
                        method, normalized_url, headers, raw_body,
                    )
                    if result is None:
                        raise rent_error("NOT_FOUND")
                    mime = result.headers.get("Content-Type", "application/octet-stream")
                    if isinstance(result.body, bytes):
                        body = _compose_i2_html(
                            result.body,
                            csrf_token=context.csrf_token if mime.startswith("text/html") else None,
                        )
                    else:
                        body = json.dumps(
                            result.body, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    self._send(result.status, body, mime, result.headers)
                    return

                if operations is not None and method in {"GET", "HEAD"} and path_only == "/rent":
                    result = operations.render(headers)
                    self._send(
                        result.status,
                        _compose_i2_html(result.body),
                        result.headers.get("Content-Type", "text/html; charset=utf-8"),
                        result.headers,
                    )
                    return

                if operations is not None and method == "GET" and path_only.startswith("/rent/data/"):
                    response = operations.data(normalized_url, headers)
                    self._send(
                        response.status,
                        json.dumps(
                            response.body, ensure_ascii=False, separators=(",", ":")
                        ).encode(),
                        response.headers.get("Content-Type", "application/json"),
                        response.headers,
                    )
                    return

                if method == "GET" and self.path == "/operator" and sessions is not None:
                    token = _cookie_token(headers.get("Cookie", headers.get("cookie", "")))
                    session = sessions.resolve(token) if token else None
                    if session is None:
                        raise rent_error("UNAUTHENTICATED")
                    page = OPERATOR_HTML.read_text(encoding="utf-8")
                    inject = "window.RENT_CSRF_TOKEN=" + json.dumps(session.csrf_token) + ";"
                    page = page.replace("const csrf=window.RENT_CSRF_TOKEN;", inject + "\nconst csrf=window.RENT_CSRF_TOKEN;")
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                    return

                raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))) if method in {"POST", "PUT", "PATCH", "DELETE"} else b""
                response = api.handle(method, self.path, headers, raw)
                self._send(
                    response.status,
                    json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode(),
                    response.headers.get("Content-Type", "application/json"),
                    response.headers,
                )
            except RentError as exc:
                self._error(exc)
            except Exception:
                self._error(rent_error("INTERNAL_ERROR"))

        def do_GET(self): self._handle("GET")
        def do_HEAD(self): self._handle("HEAD")
        def do_POST(self): self._handle("POST")
        def do_PUT(self): self._handle("PUT")
        def do_PATCH(self): self._handle("PATCH")
        def do_DELETE(self): self._handle("DELETE")
    return Handler


def local_test_http_server(
    api: RentAPI,
    sessions: RentSessionPort | None = None,
    port: int = 0,
    *,
    shell: EmptyShell | None = None,
    longstay_admin: RentLongstayAdmin | None = None,
    operations: RentOperationsUI | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        handler_class(
            api, sessions, shell, longstay_admin=longstay_admin, operations=operations
        ),
    )
    if server.server_address[0] != "127.0.0.1":
        server.server_close()
        raise RuntimeError("NON_LOOPBACK_RENT_TEST_SERVER")
    return server
