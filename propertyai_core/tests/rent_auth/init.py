"""W1-B synthetic helpers. Filename is the exact frozen ownership path (init.py)."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import InMemorySessionStore, SessionService

ORG = UUID("00000000-0000-4000-8000-000000000001")
OTHER_ORG = UUID("00000000-0000-4000-8000-000000000002")
ACTOR = UUID("00000000-0000-4000-8000-000000000003")
OTHER_ACTOR = UUID("00000000-0000-4000-8000-000000000004")
SUBJECT = "synthetic:allowlisted-operator"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def make_sessions(capabilities=("READ", "WRITE")):
    principal = AuthorizedPrincipal(ORG, ACTOR, SUBJECT, frozenset(capabilities))
    directory = ServerPrincipalDirectory([principal])
    store, clock = InMemorySessionStore(), Clock()
    sessions = SessionService(store, directory, runtime="ISOLATED_TEST",
                              ttl=timedelta(minutes=10), clock=clock)
    return sessions, store, principal, clock


def headers(issued, *, csrf=False):
    result = {"Cookie": "rent_session=" + issued.token}
    if csrf:
        result["X-CSRF-Token"] = issued.csrf_token
    return result
