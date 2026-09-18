#!/usr/bin/env python3
"""Store a local Notion token without echoing it to the terminal."""

import getpass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = ROOT / "secrets" / "notion" / "token"


def main() -> None:
    token = getpass.getpass("Notion access token (입력 숨김): ")
    if not token.strip():
        raise SystemExit("토큰이 비어 있습니다.")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = TOKEN_PATH.with_suffix(".tmp")
    temporary.write_text(token.strip() + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, TOKEN_PATH)
    print("Notion 로컬 토큰 저장 완료 (권한 600)")


if __name__ == "__main__":
    main()
