#!/usr/bin/env python3
import getpass
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / "secrets" / "telegram"
TOKEN_PATH = SECRET_DIR / "bot-token"
METADATA_PATH = ROOT / "telegram_approval" / "runtime" / "bot-metadata.json"


def atomic_private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def api(token, method):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def main():
    token = getpass.getpass("BotFather token (입력 숨김): ").strip()
    if not token or ":" not in token:
        raise SystemExit("유효한 BotFather token 형식이 아닙니다.")
    result = api(token, "getMe")
    if not result.get("ok") or not result.get("result", {}).get("is_bot"):
        raise SystemExit("Telegram getMe 검증에 실패했습니다.")
    bot = result["result"]
    atomic_private(TOKEN_PATH, token + "\n")
    atomic_private(METADATA_PATH, json.dumps({
        "schema_version": 1,
        "bot_id": bot["id"],
        "bot_username": bot.get("username"),
        "token_stored": True,
        "token_file_mode": "0o600",
        "operator_paired": False,
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"검증 완료: @{bot.get('username')}")
    print("토큰은 secrets/telegram/bot-token에 권한 600으로 저장했습니다.")


if __name__ == "__main__":
    main()
