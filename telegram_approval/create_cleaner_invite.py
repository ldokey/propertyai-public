#!/usr/bin/env python3
"""Generate a one-time Telegram cleaner onboarding URL."""

import argparse
import json

from telegram_approval.cleaner_registry import create_invite


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--party-page-id", required=True)
    parser.add_argument("--property", action="append", default=[], dest="properties")
    parser.add_argument("--priority", type=int, default=999)
    parser.add_argument("--expires-hours", type=int, default=24)
    args = parser.parse_args()
    print(json.dumps(create_invite(
        label=args.label,
        party_page_id=args.party_page_id,
        properties=args.properties,
        priority=args.priority,
        expires_hours=args.expires_hours,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
