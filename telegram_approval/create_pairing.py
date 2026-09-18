#!/usr/bin/env python3
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "telegram_approval" / "runtime" / "bot-metadata.json"
PAIRING = ROOT / "telegram_approval" / "runtime" / "pairing.json"


def atomic_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main():
    metadata = json.loads(METADATA.read_text())
    if metadata.get("operator_paired"):
        raise SystemExit("이미 운영자가 페어링되어 있습니다.")
    code = secrets.token_urlsafe(18)
    now = datetime.now(timezone.utc)
    atomic_private(PAIRING, {
        "schema_version": 1,
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "consumed": False,
    })
    print(f"https://t.me/{metadata['bot_username']}?start={code}")


if __name__ == "__main__":
    main()
