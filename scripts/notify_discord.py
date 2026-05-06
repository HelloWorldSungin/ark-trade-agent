#!/usr/bin/env python3
"""Post a message to a Discord channel via the bot API.

Reads DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID from environment. OpenClaw's
heartbeat and Hermes' proposal surface (per spec § Solo Operator Ergonomics)
both shell out to this helper so there's a single source of truth for the
outbound Discord wire.

Usage:
  scripts/notify_discord.py "message text"
  echo "message" | scripts/notify_discord.py

Env (required):
  DISCORD_BOT_TOKEN  — Discord bot token (with Send Messages permission on
                       DISCORD_CHANNEL_ID)
  DISCORD_CHANNEL_ID — target channel ID (numeric snowflake)

Exit codes:
  0 — message posted (Discord returned 200; payload echoed to stdout)
  1 — HTTP/network error talking to Discord
  2 — usage error (missing env, empty content)

Discord caps message content at 2000 characters; callers are expected to
respect that.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DISCORD_API_BASE = "https://discord.com/api/v10"


def post_message(content: str) -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token:
        print("DISCORD_BOT_TOKEN not set", file=sys.stderr)
        return 2
    if not channel_id:
        print("DISCORD_CHANNEL_ID not set", file=sys.stderr)
        return 2

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "ark-trade-agent/0.1 (notify_discord.py)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            print(
                f"posted message_id={payload.get('id')} "
                f"channel_id={payload.get('channel_id')}"
            )
            return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        print(f"discord HTTP {e.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"discord error: {e}", file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) > 1:
        content = " ".join(sys.argv[1:])
    else:
        content = sys.stdin.read().strip()
    if not content:
        print("no message content (pass as arg or via stdin)", file=sys.stderr)
        return 2
    return post_message(content)


if __name__ == "__main__":
    sys.exit(main())
