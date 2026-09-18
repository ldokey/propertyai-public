"""Disposable local browser validation server. It is never a Production entrypoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from http.server import ThreadingHTTPServer
import json
import sys
from threading import Thread

from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.web.rent_api import LocalTestSessions, RentSession, RentAPI
from propertyai_core.web.rent_server import handler_class


def main() -> None:
    with operator_fixture(today=date(2026,9,28)) as (fixture,service,_):
        sessions=LocalTestSessions()
        sessions.register("browser-synthetic-test",RentSession(service,"browser-csrf",
            datetime.now(timezone.utc)+timedelta(hours=1),frozenset({"READ","WRITE"})))
        api=RentAPI(sessions)
        Base=handler_class(api,sessions)
        class BrowserHandler(Base):
            def do_GET(self):
                if self.path=="/_synthetic_test_login":
                    self.send_response(302)
                    self.send_header("Location","/operator")
                    self.send_header("Set-Cookie","rent_session=browser-synthetic-test; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Cache-Control","no-store")
                    self.send_header("Content-Length","0")
                    self.end_headers()
                else:
                    super().do_GET()
        server=ThreadingHTTPServer(("127.0.0.1",0),BrowserHandler)
        if server.server_address[0]!="127.0.0.1":
            raise RuntimeError("NON_LOOPBACK")
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        print(json.dumps({"url":f"http://127.0.0.1:{server.server_address[1]}/_synthetic_test_login",
                          "fixture_root":str(fixture.cluster.root),
                          "login_route":"TEST_ONLY","production_effect":False}),flush=True)
        try:
            sys.stdin.readline()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__=="__main__":
    main()
