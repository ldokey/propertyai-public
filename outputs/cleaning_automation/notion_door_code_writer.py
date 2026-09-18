#!/usr/bin/env python3
"""Minimal local Notion writer for Reservation door codes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
NOTION_VERSION = "2026-03-11"


class NotionDoorCodeWriteError(RuntimeError):
    pass


def update_reservation_door_code(
    reservation_page_id: str,
    door_code: str,
    token_path: Path = TOKEN_PATH,
) -> dict:
    """Write the supplied value unchanged to Reservation.`출입 코드`."""
    token = token_path.read_text().strip()
    body = {
        "properties": {
            "출입 코드": {
                "rich_text": [{"type": "text", "text": {"content": door_code}}]
            },
            "출입정보 저장 상태": {"select": {"name": "STORED"}},
        }
    }
    request = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{reservation_page_id}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise NotionDoorCodeWriteError(type(exc).__name__) from exc
    return {"page_id": payload.get("id"), "updated": payload.get("object") == "page"}
