#!/usr/bin/env python3
"""Upload private Telegram issue photos to a Cleaning's Notion file property."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import requests

from gmail_ingest.lifecycle_actions import NOTION_TOKEN_PATH, NOTION_VERSION, _notion


def _headers(json_content=False):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN_PATH.read_text().strip()}",
        "Notion-Version": NOTION_VERSION,
        "Accept": "application/json",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def create_and_send_file(path_value):
    path = Path(path_value)
    content_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    created = requests.post(
        "https://api.notion.com/v1/file_uploads",
        headers=_headers(json_content=True),
        json={"filename": path.name, "content_type": content_type}, timeout=40,
    )
    created.raise_for_status()
    upload = created.json()
    with path.open("rb") as handle:
        sent = requests.post(
            upload["upload_url"], headers=_headers(),
            files={"file": (path.name, handle, content_type)}, timeout=120,
        )
    sent.raise_for_status()
    return upload["id"]


def attach_problem_photos(cleaning_page_id, session):
    from telegram_approval.cleaning_issue import save_session
    page = _notion("GET", f"/v1/pages/{cleaning_page_id}")
    existing = page.get("properties", {}).get("문제 사진", {}).get("files", [])
    uploaded = session.setdefault("notion_uploads", [])
    by_hash = {item["sha256"]: item for item in uploaded}
    for photo in session.get("photos", []):
        if photo["sha256"] not in by_hash:
            item = {"sha256": photo["sha256"], "filename": photo["filename"],
                    "file_upload_id": create_and_send_file(photo["path"])}
            uploaded.append(item)
            by_hash[photo["sha256"]] = item
            save_session(session)
    additions = [{
        "type": "file_upload",
        "file_upload": {"id": item["file_upload_id"]},
        "name": item["filename"],
    } for item in uploaded]
    return existing + additions
