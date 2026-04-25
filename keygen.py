"""
Generate a fresh shared secret + topic pair, and (optionally) write them
into config.json. Run this once per install.

Usage:
    python keygen.py                    # print a config snippet to stdout
    python keygen.py --write config.json   # patch config.json in place
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

NTFY_DEFAULT = "https://ntfy.sh"

DEFAULT_CATEGORIES = {
    "youtube": [
        "youtube.com", "m.youtube.com", "youtu.be",
        "youtubei.googleapis.com", "yt3.ggpht.com",
        "ytimg.com", "i.ytimg.com",
    ],
    "facebook": ["facebook.com", "m.facebook.com", "fb.com", "fbcdn.net"],
    "instagram": ["instagram.com", "cdninstagram.com"],
    "tiktok": ["tiktok.com", "tiktokcdn.com"],
    "twitter": ["twitter.com", "x.com", "t.co"],
    "reddit": ["reddit.com", "redd.it"],
    "snapchat": ["snapchat.com"],
    "twitch": ["twitch.tv"],
    "netflix": ["netflix.com"],
}


def make_config() -> dict:
    suffix = secrets.token_urlsafe(16).replace("_", "").replace("-", "")[:16]
    return {
        "ntfy_base": NTFY_DEFAULT,
        "cmd_topic": f"bk-cmd-{suffix}",
        "status_topic": f"bk-stat-{suffix}",
        "secret": secrets.token_hex(32),
        "categories": DEFAULT_CATEGORIES,
        "default_blocked": list(DEFAULT_CATEGORIES.keys()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        type=Path,
        help="write to this config.json (preserves any existing categories/default_blocked)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite secret/topics even if they already exist in --write target",
    )
    args = parser.parse_args()

    fresh = make_config()

    if args.write:
        existing: dict = {}
        if args.write.exists():
            try:
                existing = json.loads(args.write.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except json.JSONDecodeError:
                existing = {}

        merged = dict(fresh)
        # Preserve user-edited category lists.
        for k in ("categories", "default_blocked"):
            if k in existing:
                merged[k] = existing[k]
        # Don't clobber an existing secret/topic without --force.
        if not args.force:
            for k in ("cmd_topic", "status_topic", "secret", "ntfy_base"):
                if k in existing and existing[k]:
                    merged[k] = existing[k]

        args.write.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.write}", file=sys.stderr)

        # Print the controller-side snippet (everything except the categories).
        controller = {k: merged[k] for k in ("ntfy_base", "cmd_topic", "status_topic", "secret")}
        print(json.dumps(controller, indent=2))
    else:
        print(json.dumps(fresh, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
