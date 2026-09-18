from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from propertyai_core.application.rent_errors import rent_error
from propertyai_core.web.auth_session import RequestAuthenticator, session_cookie
from propertyai_core.web.empty_shell import EmptyShell, ShellSnapshot, ShellState
from propertyai_core.tests.rent_auth.init import (
    ORG, OTHER_ORG, SUBJECT, headers, make_sessions,
)


def make_shell(capabilities=("READ", "WRITE"), state=ShellState.EMPTY, loader=None):
    sessions, store, principal, clock = make_sessions(capabilities)
    shell = EmptyShell(RequestAuthenticator(sessions),
        loader if loader is not None else lambda context: ShellSnapshot(context.principal.organization_id, state))
    return shell, sessions, store, principal, clock


class Elements(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_authenticated_empty_shell_uses_only_confirmed_zero_business_state():
    observed = []
    def load(context):
        observed.append(context)
        return ShellSnapshot(context.principal.organization_id, ShellState.EMPTY)
    shell, sessions, _, principal, _ = make_shell(loader=load)
    issued = sessions.issue(SUBJECT)
    response = shell.handle("GET", "/app", headers(issued))
    page = response.body.decode()
    assert response.status == 200
    assert 'data-state="empty"' in page and 'data-empty="true"' in page
    assert "등록된 월세 업무 데이터가 없습니다." in page
    assert page.count("<strong>0건</strong>") == 3
    assert len(observed) == 1 and observed[0].principal == principal
    assert issued.token not in page and issued.session_id not in page
    assert SUBJECT not in page and str(ORG) not in page
    assert response.headers["X-Request-ID"] == observed[0].request_id
    assert response.headers["Content-Length"] == str(len(response.body))


def test_document_navigation_and_nonce_security_contract():
    shell, sessions, _, _, _ = make_shell()
    issued = sessions.issue(SUBJECT)
    first = shell.handle("GET", "/app", headers(issued))
    second = shell.handle("GET", "/app", headers(issued))
    page = first.body.decode()
    elements = Elements(page).tags
    viewport = next(attrs for tag, attrs in elements if tag == "meta" and attrs.get("name") == "viewport")
    assert viewport["content"] == "width=device-width, initial-scale=1"
    assert next(attrs for tag, attrs in elements if tag == "html")["lang"] == "ko"
    navs = [attrs["aria-label"] for tag, attrs in elements if tag == "nav"]
    assert navs == ["PC 업무 탐색", "모바일 업무 탐색"]
    links = [attrs["href"] for tag, attrs in elements if tag == "a"]
    assert set(links) == {"#content", "#rent", "#contracts", "#funding", "#settings"}
    assert '(max-width: 720px)' in page
    nonces = {attrs["nonce"] for tag, attrs in elements if tag in {"script", "style"}}
    assert len(nonces) == 1
    nonce = nonces.pop()
    assert "'nonce-" + nonce + "'" in first.headers["Content-Security-Policy"]
    assert first.headers["Content-Security-Policy"] != second.headers["Content-Security-Policy"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    for header, value in {"Cache-Control": "no-store", "Vary": "Cookie",
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}.items():
        assert first.headers[header] == value
    assert "'unsafe-inline'" not in first.headers["Content-Security-Policy"]
    assert "<form" not in page and "localStorage" not in page and "innerHTML" not in page


def test_read_only_principal_gets_no_settings_navigation():
    shell, sessions, _, _, _ = make_shell(("READ",))
    issued = sessions.issue(SUBJECT)
    response = shell.handle("GET", "/app", headers(issued))
    assert response.status == 200
    assert 'href="#settings"' not in response.body.decode()
    assert 'id="settings"' not in response.body.decode()


@pytest.mark.parametrize("capabilities", [(), ("WRITE",)])
def test_shell_rejects_no_read_capability_before_business_loader(capabilities):
    def must_not_load(_context):
        pytest.fail("Unauthorized request reached business loader")
    shell, sessions, _, _, _ = make_shell(capabilities, loader=must_not_load)
    response = shell.handle("GET", "/app", headers(sessions.issue(SUBJECT)))
    assert response.status == 403 and json.loads(response.body)["code"] == "NOT_AUTHORIZED"
    assert b"data-empty" not in response.body


def test_unauthenticated_shell_does_not_read_template_or_business_state():
    calls = []
    shell, _, _, _, _ = make_shell(loader=lambda context: calls.append(context))
    response = shell.handle("GET", "/app", {})
    assert response.status == 401 and json.loads(response.body)["code"] == "UNAUTHENTICATED"
    assert not calls and b"<html" not in response.body


@pytest.mark.parametrize("failure", ["runtime", "database", "auth", "permission", "unknown-result",
                                      "none", "dict", "cross-org"])
def test_failed_or_malformed_loader_is_never_rendered_as_ordinary_empty(failure):
    def load(_context):
        if failure == "runtime":
            raise RuntimeError("SENSITIVE_BACKEND_DIAGNOSTIC")
        if failure == "database":
            raise rent_error("RETRYABLE_TRANSACTION")
        if failure == "auth":
            raise rent_error("UNAUTHENTICATED")
        if failure == "permission":
            raise rent_error("NOT_AUTHORIZED")
        if failure == "unknown-result":
            raise rent_error("COMMIT_RESULT_UNKNOWN")
        return {"none": None, "dict": {"rows": []},
                "cross-org": ShellSnapshot(OTHER_ORG, ShellState.EMPTY)}[failure]
    shell, sessions, _, _, _ = make_shell(loader=load)
    response = shell.handle("GET", "/app", headers(sessions.issue(SUBJECT)))
    expected = {"runtime": 500, "database": 503, "auth": 401, "permission": 403,
                "unknown-result": 503, "none": 500, "dict": 500, "cross-org": 403}
    assert response.status == expected[failure]
    error = json.loads(response.body)
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert b"data-empty" not in response.body and b"<html" not in response.body
    assert b"SENSITIVE" not in response.body and "0건" not in response.body.decode()
    if failure == "unknown-result":
        assert error["state_unknown"] is True and error["retryable"] is True


def test_session_store_failure_is_system_error_not_an_empty_or_unauthenticated_screen(monkeypatch):
    shell, sessions, store, _, _ = make_shell()
    issued = sessions.issue(SUBJECT)
    def broken(_digest):
        raise OSError("SENSITIVE_SESSION_STORE_DETAILS")
    monkeypatch.setattr(store, "get", broken)
    response = shell.handle("GET", "/app", headers(issued))
    assert response.status == 500
    assert json.loads(response.body)["code"] == "INTERNAL_ERROR"
    assert b"data-empty" not in response.body and b"SENSITIVE" not in response.body


def test_loading_is_not_an_empty_or_zero_result():
    shell, sessions, _, _, _ = make_shell(state=ShellState.LOADING)
    response = shell.handle("GET", "/app", headers(sessions.issue(SUBJECT)))
    assert response.status == 200
    page = response.body.decode()
    assert 'data-state="loading"' in page and 'aria-busy="true"' in page
    assert 'data-empty="true"' not in page and "0건" not in page


def test_missing_template_is_system_error(monkeypatch, tmp_path):
    import propertyai_core.web.empty_shell as module
    shell, sessions, _, _, _ = make_shell()
    monkeypatch.setattr(module, "_TEMPLATE", tmp_path / "does-not-exist.html")
    response = shell.handle("GET", "/app", headers(sessions.issue(SUBJECT)))
    assert response.status == 500 and b"data-empty" not in response.body


@pytest.mark.parametrize("path", ["/", "/app?organization_id=spoofed", "/app#settings", "/app/",
    "https://example.invalid/app", "//example.invalid/app", "/auth/login", "/api/v1/rent/movements"])
def test_unwired_or_ambiguous_routes_do_not_pretend_to_be_available(path):
    shell, sessions, _, _, _ = make_shell()
    response = shell.handle("GET", path, headers(sessions.issue(SUBJECT)))
    assert response.status == 404 and b"data-empty" not in response.body


def test_head_preserves_authenticated_render_headers_without_body():
    shell, sessions, _, _, _ = make_shell()
    response = shell.handle("HEAD", "/app", headers(sessions.issue(SUBJECT)))
    assert response.status == 200 and response.body == b""
    assert int(response.headers["Content-Length"]) > 0


def test_logout_http_is_post_only_csrf_protected_and_actually_revokes_session():
    shell, sessions, _, _, _ = make_shell()
    issued = sessions.issue(SUBJECT)
    assert shell.handle("GET", "/auth/logout", headers(issued)).status == 404
    assert shell.handle("POST", "/auth/logout", headers(issued)).status == 403
    assert sessions.resolve(issued.token) is not None
    response = shell.handle("POST", "/auth/logout", headers(issued, csrf=True))
    assert response.status == 200 and json.loads(response.body)["status"] == "LOGGED_OUT"
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert sessions.resolve(issued.token) is None
    assert shell.handle("GET", "/app", headers(issued)).status == 401
    assert shell.handle("POST", "/auth/logout", headers(issued, csrf=True)).status == 401


def test_logout_revocation_failure_is_not_reported_as_success(monkeypatch):
    shell, sessions, store, _, _ = make_shell()
    issued = sessions.issue(SUBJECT)
    def broken(*_args):
        raise OSError("STORE_WRITE_FAILED")
    monkeypatch.setattr(store, "revoke", broken)
    response = shell.handle("POST", "/auth/logout", headers(issued, csrf=True))
    assert response.status == 500 and "Set-Cookie" not in response.headers
    assert b"LOGGED_OUT" not in response.body
    assert sessions.resolve(issued.token) is not None


def test_loader_is_mandatory_and_snapshot_is_explicit():
    sessions, _, _, _ = make_sessions()
    with pytest.raises(ValueError, match="AUTHORITATIVE_SHELL_LOADER_REQUIRED"):
        EmptyShell(RequestAuthenticator(sessions), None)
    with pytest.raises(ValueError, match="EXPLICIT_SHELL_STATE_REQUIRED"):
        ShellSnapshot(ORG, "empty")


@contextmanager
def serve_synthetic_shell(shell, sessions):
    """Owned test-only loopback bridge; never imported by application/bootstrap."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _handle(self, method):
            if method == "GET" and self.path == "/__w1b_test_login":
                issued = sessions.issue(SUBJECT)
                self.send_response(303)
                self.send_header("Set-Cookie", session_cookie(issued))
                self.send_header("Location", "/app")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            response = shell.handle(method, self.path, dict(self.headers.items()))
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_HEAD(self):
            self._handle("HEAD")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    assert server.server_address[0] == "127.0.0.1"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "W1B_TEST_HTTP_SERVER_NOT_CLEANED"


def test_real_loopback_http_401_and_success_contract():
    shell, sessions, _, _, _ = make_shell()
    issued = sessions.issue(SUBJECT)
    with serve_synthetic_shell(shell, sessions) as base:
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/app", timeout=5)
        assert error.value.code == 401
        error.value.close()
        with urlopen(Request(base + "/app", headers=headers(issued)), timeout=5) as response:
            assert response.status == 200
            assert b'data-empty="true"' in response.read()
            assert response.headers["Cache-Control"] == "no-store"


_BROWSER_SCRIPT = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [base, out, modulePath] = process.argv.slice(2);
const { chromium } = await import(pathToFileURL(modulePath).href);
const browser = await chromium.launch({headless:true});
const evidence = [];
try {
  for (const width of [320, 390, 768, 1280]) {
    const context = await browser.newContext({viewport:{width,height:844}});
    try {
      const page = await context.newPage();
      const pageErrors = [];
      page.on('pageerror', error => pageErrors.push(error.message));
      assert.equal((await page.goto(base + '/app')).status(), 401);
      const response = await page.goto(base + '/__w1b_test_login');
      assert.equal(response.status(), 200);
      assert.equal(await page.locator('#content').getAttribute('data-state'), 'empty');
      assert.equal(await page.locator('[data-empty="true"]').count(), 1);
      const layout = await page.evaluate(() => ({
        width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
        mobile:matchMedia('(max-width:720px)').matches,
        columns:getComputedStyle(document.querySelector('.frame')).gridTemplateColumns
      }));
      assert.equal(layout.width, width);
      assert.ok(layout.scrollWidth <= width, 'horizontal overflow: ' + JSON.stringify(layout));
      assert.equal(layout.mobile, width <= 720);
      if (width <= 720) {
        assert.equal(await page.locator('.desktop-nav').isVisible(), false);
        await page.getByLabel('모바일 메뉴 열기').click();
        const nav = page.getByRole('navigation', {name:'모바일 업무 탐색'});
        assert.equal(await nav.isVisible(), true);
        assert.equal(await nav.locator('a').count(), 4);
        await nav.getByRole('link', {name:'계약', exact:true}).click();
        assert.equal(new URL(page.url()).hash, '#contracts');
        assert.equal(await page.locator('.mobile-nav').getAttribute('open'), null);
      } else {
        assert.equal(await page.locator('.mobile-nav').isVisible(), false);
        const nav = page.getByRole('navigation', {name:'PC 업무 탐색'});
        assert.equal(await nav.isVisible(), true);
        assert.equal(await nav.locator('a').count(), 4);
        await nav.getByRole('link', {name:'설정', exact:true}).click();
        assert.equal(new URL(page.url()).hash, '#settings');
      }
      const cookie = (await context.cookies()).find(value => value.name === 'rent_session');
      assert.ok(cookie && cookie.httpOnly && cookie.secure && cookie.sameSite === 'Strict');
      assert.equal(await page.evaluate(() => document.cookie.includes('rent_session=')), false);
      const screenshot = out + '/shell-' + width + '.png';
      await page.screenshot({path:screenshot,fullPage:true});
      // Exercise failure UI against a controlled transport failure, not an empty success.
      await page.route('**/auth/logout', route => route.abort('failed'));
      await page.getByRole('button', {name:'로그아웃',exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#content').dataset.state === 'error');
      assert.equal(await page.locator('[data-empty="true"]').count(), 0);
      assert.equal(await page.getByRole('alert').count(), 1);
      await page.unroute('**/auth/logout');
      await page.getByRole('button', {name:'로그아웃',exact:true}).click();
      await page.getByText('로그아웃되었습니다.', {exact:true}).waitFor();
      assert.equal(await page.locator('#content').getAttribute('data-state'), 'logged-out');
      assert.equal((await context.cookies()).some(value => value.name === 'rent_session'), false);
      assert.equal((await page.goto(base + '/app')).status(), 401);
      assert.deepEqual(pageErrors, []);
      evidence.push({width,layout,authenticatedRender:'PASS',navigation:'PASS',
        hardenedCookie:'PASS',failureNotEmpty:'PASS',logoutRevocation:'PASS',screenshot,status:'PASS'});
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
fs.writeFileSync(out + '/browser-evidence.json', JSON.stringify({status:'PASS',evidence},null,2));
console.log(JSON.stringify({status:'PASS',viewports:evidence.map(value => value.width)}));
'''


def test_existing_browser_runtime_pc_mobile_navigation_logout_and_failure(tmp_path):
    """Required in the W1-B evidence run; explicit reuse, never install dependencies."""
    module = os.environ.get("PROPERTYAI_W1B_PLAYWRIGHT_ENTRY")
    if not module:
        pytest.skip("I1/browser channel requires an explicitly bound existing Playwright runtime")
    assert Path(module).is_file(), "BOUND_BROWSER_RUNTIME_MISSING"
    node = os.environ.get("PROPERTYAI_W1B_NODE") or shutil.which("node")
    assert node, "BOUND_NODE_RUNTIME_MISSING"
    shell, sessions, _, _, _ = make_shell()
    with serve_synthetic_shell(shell, sessions) as base:
        process = subprocess.run([node, "--input-type=module", "-", base, str(tmp_path), module],
            input=_BROWSER_SCRIPT, text=True, capture_output=True, timeout=120,
            env=os.environ.copy(), check=False)
    (tmp_path / "browser.stdout").write_text(process.stdout, encoding="utf-8")
    (tmp_path / "browser.stderr").write_text(process.stderr, encoding="utf-8")
    assert process.returncode == 0, process.stdout + "\n" + process.stderr
    evidence = json.loads((tmp_path / "browser-evidence.json").read_text())
    assert evidence["status"] == "PASS"
    assert [value["width"] for value in evidence["evidence"]] == [320, 390, 768, 1280]
    assert all(value["status"] == "PASS" for value in evidence["evidence"])
