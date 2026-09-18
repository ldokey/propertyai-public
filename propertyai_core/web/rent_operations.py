"""W2-B Rent operations PC/mobile UI boundary.

This module is intentionally W2-B-owned and does not modify the I1 shared router.
It consumes the I1 RentAPI authentication/command surface and authoritative Rent
read models.  Financial balances are never recalculated here: list summaries are
aggregations of ``v_rent_receivables`` effective/allocated/balance columns and
command effects are returned by the frozen application service.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any, Mapping, Type
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from propertyai_core.application.handlers.rent import RentService
from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.domain.finance import money_text
from propertyai_core.web.auth_context import RequestAuthContext
from propertyai_core.web.rent_api import APIResponse, RentAPI
from propertyai_core.web.rent_server import _safe_headers

_TEMPLATE = Path(__file__).with_name("rent_operations.html")
_MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")


@dataclass(frozen=True)
class RentOperationsResponse:
    status: int
    body: bytes
    headers: dict[str, str]


def _one(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name, [default])
    if len(values) != 1:
        raise rent_error("VALIDATION_ERROR")
    return values[0]


def _uuid_or_none(value: str) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except (TypeError, ValueError):
        raise rent_error("VALIDATION_ERROR") from None


def _positive_int(value: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise rent_error("VALIDATION_ERROR") from None
    if not minimum <= result <= maximum:
        raise rent_error("VALIDATION_ERROR")
    return result


def _iso(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _event(row: Mapping[str, Any], *, kind: str, identity_key: str, amount_key: str | None = None,
           receivable_key: str | None = "receivable_id", source_key: str | None = "source_id",
           movement_key: str | None = "movement_id", reason_key: str | None = "reason", **extra: Any) -> dict:
    amount = row.get(amount_key) if amount_key else None
    return {
        "kind": kind,
        "event_id": str(row[identity_key]),
        "occurred_at": _iso(row.get("created_at")),
        "command_id": str(row["created_command_id"]) if row.get("created_command_id") else None,
        "receivable_id": str(row[receivable_key]) if receivable_key and row.get(receivable_key) else None,
        "source_id": str(row[source_key]) if source_key and row.get(source_key) else None,
        "movement_id": str(row[movement_key]) if movement_key and row.get(movement_key) else None,
        "amount": str(int(amount)) if amount is not None else None,
        "reason": row.get(reason_key) if reason_key else None,
        **extra,
    }


class RentOperationsQueries:
    """W2-B read/view adapter over I1 authoritative application and PG read surfaces."""

    def __init__(self, service: RentService):
        self.service = service

    def overview(
        self, *, month: str, property_id: UUID | None, unit_id: UUID | None,
        search: str, page: int, page_size: int,
    ) -> dict:
        if not _MONTH.fullmatch(month):
            raise rent_error("VALIDATION_ERROR")
        if len(search) > 120 or any(ord(ch) < 32 for ch in search):
            raise rent_error("VALIDATION_ERROR")
        selected = date.fromisoformat(month + "-01")
        next_month = date(selected.year + (selected.month == 12), selected.month % 12 + 1, 1)
        needle = search.casefold().strip()

        def read(tx):
            resident_rows = tx.rows(
                """
                SELECT l.contract_id, string_agg(r.display_name, ' · ' ORDER BY r.display_name, r.resident_id) AS resident_names
                  FROM propertyai.rent_contract_resident l
                  JOIN propertyai.rent_resident r
                    ON (r.organization_id,r.resident_id)=(l.organization_id,l.resident_id)
                 WHERE l.organization_id=%s
                 GROUP BY l.contract_id
                """,
                (tx.organization_id,),
            )
            residents = {row["contract_id"]: (row["resident_names"] or "") for row in resident_rows}
            rows = tx.rows(
                """
                SELECT r.*, p.cycle_start, p.cycle_end_exclusive,
                       c.rental_unit_id, u.property_name, u.unit_name, u.timezone_name
                  FROM propertyai.v_rent_receivables r
                  JOIN propertyai.v_rent_periods p
                    ON (p.organization_id,p.period_id)=(r.organization_id,r.period_id)
                  JOIN propertyai.v_rent_contracts c
                    ON (c.organization_id,c.contract_id)=(r.organization_id,r.contract_id)
                  JOIN propertyai.v_rent_reference_units u
                    ON (u.organization_id,u.property_id,u.rental_unit_id)=
                       (c.organization_id,c.property_id,c.rental_unit_id)
                 ORDER BY r.due_on, r.receivable_id
                """
            )

            def matches(row: Mapping[str, Any]) -> bool:
                if property_id is not None and row["property_id"] != property_id:
                    return False
                if unit_id is not None and row["rental_unit_id"] != unit_id:
                    return False
                if needle:
                    haystack = " ".join((
                        str(row["receivable_id"]), str(row["contract_id"]),
                        str(row["property_name"]), str(row["unit_name"]),
                        residents.get(row["contract_id"], ""),
                    )).casefold()
                    if needle not in haystack:
                        return False
                return True

            filtered_all = [row for row in rows if matches(row)]
            filtered_month = [
                row for row in filtered_all
                if row["cycle_start"] < next_month and selected < row["cycle_end_exclusive"]
            ]
            total_rows = len(filtered_month)
            start = (page - 1) * page_size
            page_rows = filtered_month[start:start + page_size]
            wire_rows = []
            for row in page_rows:
                today = self.service.business_date.today(row["timezone_name"])
                base = self.service._receivable_row(row, today)  # consume frozen P1 presentation semantics
                wire_rows.append({
                    **base,
                    "rental_unit_id": str(row["rental_unit_id"]),
                    "property_name": row["property_name"],
                    "unit_name": row["unit_name"],
                    "resident_names": residents.get(row["contract_id"], ""),
                    "cycle_start": row["cycle_start"].isoformat(),
                    "cycle_end_exclusive": row["cycle_end_exclusive"].isoformat(),
                })

            def total(items: list[Mapping[str, Any]], column: str) -> str:
                # Values are already authoritative read-model balances; this only aggregates the query set.
                return money_text(sum(int(row[column]) for row in items))

            total_pages = max(1, (total_rows + page_size - 1) // page_size)
            return {
                "as_of": datetime.now().astimezone().isoformat(),
                "selected_month": month,
                "filters": {
                    "property_id": str(property_id) if property_id else None,
                    "unit_id": str(unit_id) if unit_id else None,
                    "search": search,
                },
                "summary": {
                    "selected_month_obligation": total(filtered_month, "effective_amount"),
                    "selected_month_allocated": total(filtered_month, "allocated"),
                    "selected_month_outstanding": total(filtered_month, "balance"),
                    "all_period_outstanding": total(filtered_all, "balance"),
                    "summary_scope": "WHOLE_FILTERED_QUERY_NOT_PAGE",
                },
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_rows": total_rows,
                    "total_pages": total_pages,
                    "page_rows": len(wire_rows),
                },
                "rows": wire_rows,
            }
        return self.service.repository.read_snapshot(read)

    def context(self) -> dict:
        reference = self.service.reference_data()
        sources = self.service.funding_sources()
        revision = self.service.repository.read_operator_snapshot(lambda tx: str(tx.ledger_revision_before))
        return {
            "ledger_revision": revision,
            "reference_data": reference,
            "funding_sources": sources,
            "record_refund_semantics": "RECORD_EXISTING_EXTERNAL_TRANSFER_ONLY",
            "automatic_allocation": False,
        }

    def ledger(self, *, receivable_id: UUID | None = None, source_id: UUID | None = None) -> dict:
        def read(tx):
            receivables = tx.rows(
                "SELECT * FROM propertyai.finance_receivable ORDER BY created_at,receivable_id"
            )
            adjustments = tx.rows(
                "SELECT * FROM propertyai.finance_receivable_adjustment ORDER BY created_at,adjustment_id"
            )
            allocations = tx.rows(
                "SELECT * FROM propertyai.finance_allocation ORDER BY created_at,allocation_id"
            )
            sources = tx.rows(
                "SELECT * FROM propertyai.finance_funding_source ORDER BY created_at,source_id"
            )
            movements = tx.rows(
                """
                SELECT m.movement_id,m.direction,m.replaces_movement_id,m.current_revision,
                       r.movement_revision_id,r.created_command_id,r.created_at,r.revision_no,
                       r.previous_revision_no,r.account_id,r.occurred_on,r.amount,r.currency,
                       r.payer_raw,r.record_status,r.reason
                  FROM propertyai.finance_movement m
                  JOIN propertyai.finance_movement_revision r
                    ON (r.organization_id,r.movement_id)=(m.organization_id,m.movement_id)
                 ORDER BY r.created_at,r.movement_revision_id
                """
            )
            returns = tx.rows(
                "SELECT * FROM propertyai.finance_source_return ORDER BY created_at,return_id"
            )
            current_receivables = tx.rows(
                "SELECT * FROM propertyai.v_rent_receivables ORDER BY due_on,receivable_id"
            )
            current_sources = tx.rows(
                "SELECT * FROM propertyai.v_rent_sources ORDER BY source_id"
            )

            allocation_source = {row["source_id"] for row in allocations if receivable_id is None or row["receivable_id"] == receivable_id}
            target_sources = {source_id} if source_id else set()
            if receivable_id is not None:
                target_sources |= allocation_source
            source_movement = {row["source_id"]: row["movement_id"] for row in sources}
            target_movements = {source_movement[s] for s in target_sources if s in source_movement}

            events: list[dict] = []
            for row in receivables:
                if receivable_id is not None and row["receivable_id"] != receivable_id:
                    continue
                events.append(_event(
                    row, kind="RECEIVABLE", identity_key="receivable_id", amount_key="original_amount",
                    source_key=None, movement_key=None, reason_key="void_reason",
                    record_kind="VOID" if row["voided"] else "ISSUED",
                    replacement_of=str(row["replacement_of"]) if row["replacement_of"] else None,
                ))
            for row in adjustments:
                if receivable_id is not None and row["receivable_id"] != receivable_id:
                    continue
                events.append(_event(
                    row, kind="RECEIVABLE_ADJUSTMENT", identity_key="adjustment_id", amount_key="delta",
                    source_key=None, movement_key=None,
                    reverse_of=str(row["reverse_of"]) if row["reverse_of"] else None,
                ))
            for row in allocations:
                if receivable_id is not None and row["receivable_id"] != receivable_id:
                    continue
                if source_id is not None and row["source_id"] != source_id:
                    continue
                events.append(_event(
                    row, kind="ALLOCATION", identity_key="allocation_id", amount_key="amount",
                    movement_key=None, record_kind=row["record_kind"],
                    reverse_of=str(row["reverse_of"]) if row["reverse_of"] else None,
                ))
            for row in movements:
                source_for_movement = next((s for s, m in source_movement.items() if m == row["movement_id"]), None)
                if receivable_id is not None and row["movement_id"] not in target_movements:
                    continue
                if source_id is not None and source_for_movement != source_id:
                    continue
                events.append(_event(
                    row, kind="MOVEMENT_REVISION", identity_key="movement_revision_id", amount_key="amount",
                    receivable_key=None, source_key=None, reason_key="reason",
                    source_id=str(source_for_movement) if source_for_movement else None,
                    direction=row["direction"], record_status=row["record_status"],
                    revision_no=row["revision_no"], previous_revision_no=row["previous_revision_no"],
                    occurred_on=row["occurred_on"].isoformat(),
                    replaces_movement_id=str(row["replaces_movement_id"]) if row["replaces_movement_id"] else None,
                ))
            for row in returns:
                if source_id is not None and row["source_id"] != source_id:
                    continue
                if receivable_id is not None and row["source_id"] not in target_sources:
                    continue
                events.append(_event(
                    row, kind="REFUND_RECORD", identity_key="return_id", amount_key="amount",
                    receivable_key=None, movement_key="outgoing_movement_id",
                    record_kind=row["record_kind"],
                    reverse_of=str(row["reverse_of"]) if row["reverse_of"] else None,
                    actual_transfer_confirmed=bool(row["actual_transfer_confirmed"]),
                ))
            events.sort(key=lambda item: (item["occurred_at"] or "", item["kind"], item["event_id"]))

            current_receivable = next(
                (row for row in current_receivables if receivable_id is not None and row["receivable_id"] == receivable_id),
                None,
            )
            current_source = next(
                (row for row in current_sources if source_id is not None and row["source_id"] == source_id),
                None,
            )
            return {
                "receivable_id": str(receivable_id) if receivable_id else None,
                "source_id": str(source_id) if source_id else None,
                "current_receivable": None if current_receivable is None else {
                    "effective_amount": money_text(int(current_receivable["effective_amount"])),
                    "allocated": money_text(int(current_receivable["allocated"])),
                    "outstanding": money_text(int(current_receivable["balance"])),
                    "version": str(current_receivable["version"]),
                    "voided": bool(current_receivable["voided"]),
                },
                "current_source": None if current_source is None else {
                    "principal": money_text(int(current_source["principal"])),
                    "allocated": money_text(int(current_source["allocated"])),
                    "returned": money_text(int(current_source["returned"])),
                    "available": money_text(int(current_source["available"])),
                    "version": str(current_source["version"]),
                    "attribution_status": current_source["attribution_status"],
                },
                "events": events,
            }
        return self.service.repository.read_snapshot(read)


class RentOperationsUI:
    """Dedicated W2-B UI adapter. Shared-router mounting is deliberately deferred to I2."""

    def __init__(self, api: RentAPI):
        if not api.integrated_auth:
            raise ValueError("W2_B_INTEGRATED_AUTH_REQUIRED")
        self.api = api

    def _authorized(self, method: str, headers: Mapping[str, str], capability: str) -> tuple[RentService, RequestAuthContext]:
        service, _capabilities, context = self.api._scope(method, headers, capability)
        if context is None or not isinstance(service, RentService):
            raise rent_error("NOT_AUTHORIZED")
        return service, context

    def render(self, headers: Mapping[str, str]) -> RentOperationsResponse:
        _service, context = self._authorized("GET", headers, "READ")
        bootstrap = json.dumps(
            {
                "csrf_token": context.csrf_token,
                "request_id": context.request_id,
                "routes": {
                    "overview": "/rent/data/overview",
                    "context": "/rent/data/context",
                    "ledger": "/rent/data/ledger",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).replace("<", "\\u003c")
        page = _TEMPLATE.read_text(encoding="utf-8").replace("__RENT_BOOTSTRAP__", bootstrap)
        return RentOperationsResponse(200, page.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})

    def data(self, raw_url: str, headers: Mapping[str, str]) -> APIResponse:
        request_id = str(uuid4())
        try:
            service, context = self._authorized("GET", headers, "READ")
            request_id = context.request_id
            parsed = urlsplit(raw_url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            queries = RentOperationsQueries(service)
            if parsed.path == "/rent/data/overview":
                month = _one(query, "month")
                result = queries.overview(
                    month=month,
                    property_id=_uuid_or_none(_one(query, "property_id")),
                    unit_id=_uuid_or_none(_one(query, "unit_id")),
                    search=_one(query, "search"),
                    page=_positive_int(_one(query, "page", "1"), minimum=1, maximum=1_000_000),
                    page_size=_positive_int(_one(query, "page_size", "20"), minimum=1, maximum=100),
                )
            elif parsed.path == "/rent/data/context":
                result = queries.context()
            elif parsed.path == "/rent/data/ledger":
                result = queries.ledger(
                    receivable_id=_uuid_or_none(_one(query, "receivable_id")),
                    source_id=_uuid_or_none(_one(query, "source_id")),
                )
            else:
                raise rent_error("NOT_FOUND")
            return APIResponse(200, result, {"Content-Type": "application/json", "X-Request-ID": request_id})
        except RentError as exc:
            return APIResponse(exc.status, exc.wire(request_id), {"Content-Type": "application/json", "X-Request-ID": request_id})
        except Exception:
            exc = rent_error("INTERNAL_ERROR")
            return APIResponse(exc.status, exc.wire(request_id), {"Content-Type": "application/json", "X-Request-ID": request_id})


def rent_operations_handler_class(ui: RentOperationsUI) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _send(self, status: int, content: bytes, mime: str, extra: Mapping[str, str] | None = None) -> None:
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

        def _wire_error(self, exc: RentError) -> None:
            request_id = str(uuid4())
            body = json.dumps(exc.wire(request_id), separators=(",", ":")).encode()
            self._send(exc.status, body, "application/json", {"X-Request-ID": request_id})

        def _handle(self, method: str) -> None:
            try:
                headers = _safe_headers(self.headers)
                parsed = urlsplit(self.path)
                if method in {"GET", "HEAD"} and parsed.path == "/rent":
                    response = ui.render(headers)
                    self._send(response.status, response.body, response.headers["Content-Type"], response.headers)
                    return
                if method == "GET" and parsed.path.startswith("/rent/data/"):
                    response = ui.data(self.path, headers)
                    self._send(
                        response.status,
                        json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode(),
                        response.headers.get("Content-Type", "application/json"),
                        response.headers,
                    )
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))) if method in {"POST", "PUT", "PATCH", "DELETE"} else b""
                response = ui.api.handle(method, self.path, headers, raw)
                self._send(
                    response.status,
                    json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode(),
                    response.headers.get("Content-Type", "application/json"),
                    response.headers,
                )
            except RentError as exc:
                self._wire_error(exc)
            except Exception:
                self._wire_error(rent_error("INTERNAL_ERROR"))

        def do_GET(self): self._handle("GET")
        def do_HEAD(self): self._handle("HEAD")
        def do_POST(self): self._handle("POST")
        def do_PUT(self): self._handle("PUT")
        def do_PATCH(self): self._handle("PATCH")
        def do_DELETE(self): self._handle("DELETE")

    return Handler


def local_rent_operations_http_server(ui: RentOperationsUI, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), rent_operations_handler_class(ui))
    if server.server_address[0] != "127.0.0.1":
        server.server_close()
        raise RuntimeError("NON_LOOPBACK_W2_B_TEST_SERVER")
    return server
