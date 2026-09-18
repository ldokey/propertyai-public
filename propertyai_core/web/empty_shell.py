"""Authenticated responsive shell; no Finance logic, router wiring, or DB access.

The required loader must attest EMPTY for this organization after a successful
read. Missing/malformed results and failures NEVER become a normal empty state.
I1 owns the shared router and real loader; no default lambda returning empty exists.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from html import escape
import json
from pathlib import Path
import secrets
from string import Template
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.auth_context import RequestAuthContext, require_authorized
from propertyai_core.web.auth_session import RequestAuthenticator, clear_session_cookie


class ShellState(Enum):
    EMPTY = "empty"
    LOADING = "loading"


@dataclass(frozen=True, slots=True)
class ShellSnapshot:
    organization_id: UUID
    state: ShellState

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, UUID) or self.organization_id.int == 0:
            raise ValueError("INVALID_SHELL_ORGANIZATION")
        if not isinstance(self.state, ShellState):
            raise ValueError("EXPLICIT_SHELL_STATE_REQUIRED")


@dataclass(frozen=True, slots=True)
class ShellResponse:
    status: int
    body: bytes
    headers: dict[str, str]


# Navigation boundaries only. No links to unimplemented money commands or imports.
_NAVIGATION = (
    ("rent", "월세", frozenset({"READ"})),
    ("contracts", "계약", frozenset({"READ"})),
    ("funding", "미배분", frozenset({"READ"})),
    ("settings", "설정", frozenset({"READ", "WRITE"})),
)
_TEMPLATE = Path(__file__).with_suffix(".html")


def _response(status: int, body: bytes, request_id: str, *, html: bool = False,
              nonce: str | None = None, extra: Mapping[str, str] | None = None) -> ShellResponse:
    csp = ("default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
           "form-action 'none'; object-src 'none'; connect-src 'self'")
    if nonce is not None:
        csp += f"; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'"
    headers = {
        "Content-Type": "text/html; charset=utf-8" if html else "application/json",
        "Content-Length": str(len(body)), "Cache-Control": "no-store",
        "Vary": "Cookie", "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer", "Content-Security-Policy": csp,
    }
    headers.update(extra or {})
    return ShellResponse(status, body, headers)


def _json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class EmptyShell:
    def __init__(self, auth: RequestAuthenticator,
                 load_state: Callable[[RequestAuthContext], ShellSnapshot]):
        if not callable(load_state):
            raise ValueError("AUTHORITATIVE_SHELL_LOADER_REQUIRED")
        self.auth, self.load_state = auth, load_state

    def handle(self, method: str, raw_url: str, headers: Mapping[str, str]) -> ShellResponse:
        request_id = str(uuid4())
        try:
            # Authenticate even invalid routes; never render template bytes first.
            context = self.auth.authenticate(method, headers)
            request_id = context.request_id
            method = method.upper()
            if not isinstance(raw_url, str) or not raw_url.startswith("/") or raw_url.startswith("//"):
                raise rent_error("NOT_FOUND")
            url = urlsplit(raw_url)
            if url.query or url.fragment:
                raise rent_error("NOT_FOUND")
            if (method, url.path) == ("POST", "/auth/logout"):
                self.auth.sessions.revoke(context.session_id)
                return _response(200, _json({"status": "LOGGED_OUT", "request_id": request_id}),
                                 request_id, extra={"Set-Cookie": clear_session_cookie()})
            if method not in {"GET", "HEAD"} or url.path != "/app":
                raise rent_error("NOT_FOUND")
            require_authorized(context.principal, capabilities={"READ"})
            snapshot = self.load_state(context)
            if not isinstance(snapshot, ShellSnapshot):
                raise rent_error("INTERNAL_ERROR")
            require_authorized(context.principal, organization_id=snapshot.organization_id)
            body, nonce = self._render(context, snapshot)
            result = _response(200, body, request_id, html=True, nonce=nonce)
            return result if method == "GET" else ShellResponse(result.status, b"", result.headers)
        except RentError as exc:
            return _response(exc.status, _json(exc.wire(request_id)), request_id)
        except Exception:
            exc = rent_error("INTERNAL_ERROR")
            return _response(exc.status, _json(exc.wire(request_id)), request_id)

    def _render(self, context: RequestAuthContext, snapshot: ShellSnapshot) -> tuple[bytes, str]:
        allowed = [item for item in _NAVIGATION if item[2].issubset(context.principal.capabilities)]
        navigation = "".join(f'<a href="#{key}" data-nav="{key}">{escape(label)}</a>'
                             for key, label, _ in allowed)
        if snapshot.state is ShellState.EMPTY:
            body = ('<section id="rent" aria-labelledby="rent-title" data-empty="true">'
                    '<h2 id="rent-title">월세</h2><p class="state-message" role="status">'
                    '등록된 월세 업무 데이터가 없습니다.</p>'
                    '<div class="summary" aria-label="빈 업무 현황">'
                    '<div><span>계약</span><strong>0건</strong></div>'
                    '<div><span>청구</span><strong>0건</strong></div>'
                    '<div><span>기록된 입금</span><strong>0건</strong></div></div>'
                    '<p>업무 기능은 다음 통합 단계에서 연결됩니다. 샘플 데이터는 생성하지 않습니다.</p></section>')
        elif snapshot.state is ShellState.LOADING:
            body = ('<section id="rent" aria-labelledby="rent-title" aria-busy="true">'
                    '<h2 id="rent-title">월세</h2><p role="status">업무 상태를 확인하고 있습니다.</p>'
                    '<p>확인이 끝나기 전에는 빈 데이터나 잔액으로 표시하지 않습니다.</p></section>')
        else:
            raise rent_error("INTERNAL_ERROR")
        for key, label, _ in allowed:
            if key != "rent":
                body += (f'<section id="{key}" aria-labelledby="{key}-title">'
                         f'<h2 id="{key}-title">{escape(label)}</h2>'
                         '<p>탐색 경계만 준비되어 있습니다. 업무 화면은 아직 연결되지 않았습니다.</p></section>')
        nonce = secrets.token_urlsafe(24)
        page = Template(_TEMPLATE.read_text(encoding="utf-8")).substitute(
            nonce=nonce, csrf=escape(context.csrf_token, quote=True),
            navigation=navigation, body=body, state=snapshot.state.value,
            request_id=escape(context.request_id, quote=True),
        )
        return page.encode("utf-8"), nonce
