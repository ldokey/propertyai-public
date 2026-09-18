#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "secrets" / "google"
CREDENTIALS = SECRETS / "credentials.json"
TOKEN = SECRETS / "token.json"
METADATA = ROOT / "gmail_ingest" / "runtime" / "token-metadata.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def atomic_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def connect():
    if not CREDENTIALS.exists():
        raise FileNotFoundError(f"OAuth credentials missing: {CREDENTIALS}")
    credentials = None
    if TOKEN.exists():
        token_payload = json.loads(TOKEN.read_text())
        stored_scopes = set(token_payload.get("scopes", []))
        if set(SCOPES).issubset(stored_scopes):
            credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
        TOKEN.write_text(credentials.to_json())
        os.chmod(TOKEN, 0o600)

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    account_hash = hashlib.sha256(profile["emailAddress"].encode()).hexdigest()
    metadata = {
        "schema_version": 1,
        "scope": SCOPES,
        "account_ref_hash": account_hash,
        "history_id_present": bool(profile.get("historyId")),
        "refresh_token_present": bool(credentials.refresh_token),
        "token_file_mode": oct(TOKEN.stat().st_mode & 0o777),
        "email_address_stored": False,
    }
    atomic_private_json(METADATA, metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    connect()
