"""
Windows site blocker with remote unlock.

Polls a small JSON document (GitHub Gist by default) for lock state and
keeps the Windows hosts file in sync. Runs as a long-lived process under
a Scheduled Task with SYSTEM privileges so it survives reboots and
cannot be stopped from a standard user account.

Remote JSON format:
    {"locked": true}
or to temporarily unlock until a wall-clock time (UTC):
    {"locked": false, "until": "2026-04-24T22:00:00Z"}

When "until" is in the past the client treats the state as locked again
so an unlock window auto-expires even if the remote file is never
updated afterwards.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
BEGIN_MARKER = "# BEGIN BLOCKYOUTUBE"
END_MARKER = "# END BLOCKYOUTUBE"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOG_PATH = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlockYouTube" / "blocker.log"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(console)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fetch_remote_state(url: str, timeout: float) -> dict | None:
    cache_bust = f"{'&' if '?' in url else '?'}t={int(time.time())}"
    req = urllib.request.Request(
        url + cache_bust,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "blockyoutube/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logging.warning("remote fetch failed: %s", exc)
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logging.warning("remote payload not valid JSON: %r", raw[:200])
        return None


def interpret_state(state: dict | None) -> bool:
    """Return True when sites should be blocked."""
    if not isinstance(state, dict):
        return True

    locked = bool(state.get("locked", True))
    if locked:
        return True

    until = state.get("until")
    if not until:
        return False

    try:
        deadline = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        logging.warning("bad 'until' value: %r — treating as locked", until)
        return True

    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) >= deadline


def build_block_lines(domains: list[str]) -> list[str]:
    lines = [BEGIN_MARKER]
    for domain in domains:
        domain = domain.strip().lower()
        if not domain:
            continue
        lines.append(f"0.0.0.0 {domain}")
        if not domain.startswith("www."):
            lines.append(f"0.0.0.0 www.{domain}")
    lines.append(END_MARKER)
    return lines


def read_hosts() -> list[str]:
    try:
        return HOSTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def write_hosts(lines: list[str]) -> None:
    tmp = HOSTS_PATH.with_suffix(".blockyoutube.tmp")
    content = "\r\n".join(lines)
    if not content.endswith("\r\n"):
        content += "\r\n"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, HOSTS_PATH)


def strip_block_section(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.strip() == BEGIN_MARKER:
            in_section = True
            continue
        if line.strip() == END_MARKER:
            in_section = False
            continue
        if not in_section:
            out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def apply_state(block: bool, domains: list[str]) -> bool:
    """Sync the hosts file with the desired state. Returns True if changed."""
    current = read_hosts()
    base = strip_block_section(current)

    if block:
        desired = base + [""] + build_block_lines(domains)
    else:
        desired = base

    if desired == current:
        return False

    write_hosts(desired)
    return True


def flush_dns() -> None:
    try:
        subprocess.run(
            ["ipconfig", "/flushdns"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        pass


def have_network() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
        return True
    except OSError:
        return False


def run_loop(config: dict) -> None:
    url = config["remote_url"]
    domains = config["blocked_domains"]
    poll = int(config.get("poll_seconds", 90))
    timeout = float(config.get("http_timeout_seconds", 10))
    offline_policy_locked = bool(config.get("offline_is_locked", True))

    logging.info("starting; poll=%ss domains=%d", poll, len(domains))
    last_block_state: bool | None = None

    while True:
        if have_network():
            state = fetch_remote_state(url, timeout)
            if state is None:
                block = offline_policy_locked if last_block_state is None else last_block_state
            else:
                block = interpret_state(state)
        else:
            block = offline_policy_locked if last_block_state is None else last_block_state

        try:
            changed = apply_state(block, domains)
        except PermissionError:
            logging.error("cannot write hosts file — process needs admin/SYSTEM privileges")
            time.sleep(poll)
            continue
        except Exception as exc:
            logging.exception("apply_state failed: %s", exc)
            time.sleep(poll)
            continue

        if changed:
            logging.info("hosts updated: blocked=%s", block)
            flush_dns()
        elif last_block_state != block:
            logging.info("state confirmed: blocked=%s", block)

        last_block_state = block
        time.sleep(poll)


def main() -> int:
    setup_logging()
    if os.name != "nt":
        logging.error("this script only runs on Windows")
        return 2
    if not is_admin():
        logging.error("must run as Administrator / SYSTEM")
        return 3

    try:
        config = load_config()
    except FileNotFoundError:
        logging.error("config.json missing at %s", CONFIG_PATH)
        return 4
    except json.JSONDecodeError as exc:
        logging.error("config.json invalid: %s", exc)
        return 4

    if not config.get("remote_url") or "YOUR_GIST" in config["remote_url"]:
        logging.error("config.json: remote_url is not set")
        return 5

    try:
        run_loop(config)
    except KeyboardInterrupt:
        logging.info("stopped by user")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
