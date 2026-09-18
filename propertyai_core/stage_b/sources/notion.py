from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..models import SnapshotIntegrityError, SourceObservation
from ..normalize import normalize_datetime, normalize_page_uuid


@dataclass(frozen=True)
class NotionPage:
    rows: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None
    has_more: bool = False


class NotionSnapshotSource:
    """Complete paginated read with exact duplicate and cursor-loop rejection."""

    def __init__(
        self,
        source_type: str,
        fetch_page: Callable[[str | None], NotionPage | Mapping[str, Any]],
        fetch_row: Callable[[str], Mapping[str, Any] | None],
        *,
        semantic_normalizer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        runtime_reference: str,
        max_pages: int = 10_000,
    ) -> None:
        self.source_type = source_type
        self._fetch_page = fetch_page
        self._fetch_row = fetch_row
        self._semantic_normalizer = semantic_normalizer
        self.runtime_reference = runtime_reference
        self.max_pages = max_pages

    @staticmethod
    def _coerce_page(value: NotionPage | Mapping[str, Any]) -> NotionPage:
        if isinstance(value, NotionPage):
            return value
        raw_rows = value.get("results", value.get("rows", ()))
        return NotionPage(
            rows=tuple(raw_rows),
            next_cursor=value.get("next_cursor"),
            has_more=bool(value.get("has_more", False)),
        )

    def _observation(self, row: Mapping[str, Any]) -> SourceObservation:
        if "id" not in row:
            raise SnapshotIntegrityError("Notion row lacks page id")
        page_id = normalize_page_uuid(str(row["id"]))
        edited: datetime | None = None
        if row.get("last_edited_time") is not None:
            edited = normalize_datetime(row["last_edited_time"])
        return SourceObservation(
            source_type=self.source_type,
            durable_identity=page_id,
            semantic_payload=self._semantic_normalizer(row),
            source_ref=str(row.get("url") or page_id),
            last_edited_time=edited,
        )

    def scan(self) -> tuple[SourceObservation, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        observations: dict[str, SourceObservation] = {}
        for _ in range(self.max_pages):
            page = self._coerce_page(self._fetch_page(cursor))
            for raw_row in page.rows:
                observation = self._observation(raw_row)
                if observation.row_key in observations:
                    raise SnapshotIntegrityError(
                        f"duplicate Notion source row: {observation.row_key}"
                    )
                observations[observation.row_key] = observation
            if not page.has_more:
                if page.next_cursor is not None:
                    raise SnapshotIntegrityError("terminal Notion page returned a next cursor")
                return tuple(observations[key] for key in sorted(observations))
            if not page.next_cursor:
                raise SnapshotIntegrityError("paginated Notion response omitted next cursor")
            if page.next_cursor in seen_cursors or page.next_cursor == cursor:
                raise SnapshotIntegrityError("Notion pagination cursor repeated")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        raise SnapshotIntegrityError("Notion pagination exceeded bounded maximum")

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        row = self._fetch_row(normalize_page_uuid(durable_identity))
        return None if row is None else self._observation(row)

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return {}


__all__ = ["NotionPage", "NotionSnapshotSource"]
