"""
Command-line controller. Sends signed commands to the Windows agent
via ntfy. Useful for testing the round-trip without the phone PWA, and
for scripting from any machine that has Python + your secret.

Examples:
    python cli.py --config controller-config.json block
    python cli.py --config controller-config.json unblock --for 30m
    python cli.py --config controller-config.json unblock --only youtube --until 2026-04-25T22:00Z
    python cli.py --config controller-config.json status
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def parse_duration(s: str) -> timedelta:
    m = DURATION_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {s!r} (use e.g. 30m, 2h)")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def canonical(cmd: dict) -> bytes:
    body = {k: v for k, v in cmd.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(secret_hex: str, cmd: dict) -> str:
    return hmac.new(
        bytes.fromhex(secret_hex), canonical(cmd), hashlib.sha256
    ).hexdigest()


def post(url: str, body: bytes) -> None:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "blockyoutube-cli/2.0",
            "Title": "BlockYouTube",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def fetch_latest_status(base: str, topic: str, timeout: float = 5.0) -> dict | None:
    url = f"{base.rstrip('/')}/{urllib.parse.quote(topic)}/json?poll=1"
    req = urllib.request.Request(
        url, headers={"User-Agent": "blockyoutube-cli/2.0"}
    )
    latest: dict | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("event") == "message":
                    try:
                        latest = json.loads(evt.get("message", ""))
                    except json.JSONDecodeError:
                        latest = {"raw": evt.get("message")}
    except urllib.error.URLError as exc:
        print(f"status fetch failed: {exc}", file=sys.stderr)
        return None
    return latest


def load_controller_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    for k in ("cmd_topic", "secret"):
        if k not in cfg:
            raise SystemExit(f"controller config missing '{k}'")
    return cfg


def cmd_block(cfg: dict) -> dict:
    return {"cmd": "block", "nonce": int(time.time() * 1000)}


def cmd_unblock(cfg: dict, allow: list[str] | None, until: datetime | None) -> dict:
    body: dict = {"cmd": "unblock", "nonce": int(time.time() * 1000)}
    if allow is not None:
        body["allow"] = allow
    if until is not None:
        body["until"] = until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True,
                        help="path to a controller-config.json containing cmd_topic + secret")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("block", help="block all default categories")

    p_unblock = sub.add_parser("unblock", help="unblock; specify scope and duration")
    p_unblock.add_argument("--only", action="append", default=None,
                           help="category to keep allowed (repeatable). Default: all categories.")
    grp = p_unblock.add_mutually_exclusive_group()
    grp.add_argument("--for", dest="for_", type=parse_duration,
                     help="auto-relock after this duration, e.g. 30m, 2h, 1d")
    grp.add_argument("--until", dest="until_iso",
                     help="auto-relock at this UTC ISO timestamp, e.g. 2026-04-25T22:00Z")

    sub.add_parser("status", help="poll the status topic for the latest agent heartbeat")

    args = parser.parse_args()
    cfg = load_controller_config(args.config)
    base = cfg.get("ntfy_base", "https://ntfy.sh").rstrip("/")

    if args.action == "status":
        status_topic = cfg.get("status_topic")
        if not status_topic:
            print("controller config has no status_topic", file=sys.stderr)
            return 2
        latest = fetch_latest_status(base, status_topic)
        if latest is None:
            print("no status received yet")
            return 1
        print(json.dumps(latest, indent=2))
        return 0

    if args.action == "block":
        body = cmd_block(cfg)
    elif args.action == "unblock":
        until_dt: datetime | None = None
        if args.for_:
            until_dt = datetime.now(timezone.utc) + args.for_
        elif args.until_iso:
            until_dt = datetime.fromisoformat(args.until_iso.replace("Z", "+00:00"))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
        body = cmd_unblock(cfg, allow=args.only, until=until_dt)
    else:
        parser.error(f"unknown action: {args.action}")
        return 2

    body["sig"] = sign(cfg["secret"], body)
    url = f"{base}/{urllib.parse.quote(cfg['cmd_topic'])}"
    post(url, json.dumps(body).encode())
    print(json.dumps(body, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
