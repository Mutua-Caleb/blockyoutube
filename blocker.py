"""
Windows site blocker with remote unlock.

Run as a one-shot from a Scheduled Task. Two task instances:

  * boot-time:  blocker.py --lock-only   (no network call; writes the
                block to the hosts file so YouTube is unreachable from
                the moment the box finishes booting)
  * every 5m:   blocker.py               (poll the gist; either keep
                the block or remove it if the gist says unlocked)

Both invocations exit within seconds. There is no long-running loop —
the OS scheduler is the loop. If the sync task ever fails, the block
stays in place; the only thing the sync task can do is *unlock*.

Remote JSON format:
    {"locked": true}
or to temporarily unlock until a wall-clock time (UTC):
    {"locked": false, "until": "2026-04-24T22:00:00Z"}
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
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


def whoami() -> str:
    try:
        out = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (out.stdout or out.stderr or "").strip() or "?"
    except Exception as exc:
        return f"?({exc})"


def probe_hosts_write() -> str:
    """Return a short description of why a hosts write would fail, or 'ok'."""
    try:
        with HOSTS_PATH.open("a", encoding="utf-8"):
            pass
    except PermissionError as exc:
        return f"open(append) PermissionError winerror={getattr(exc, 'winerror', None)} {exc}"
    except Exception as exc:
        return f"open(append) {type(exc).__name__}: {exc}"
    return "ok"


def fetch_remote_state(url: str, timeout: float) -> dict | None:
    import time as _time
    cache_bust = f"{'&' if '?' in url else '?'}t={int(_time.time())}"
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
    content = "\r\n".join(lines)
    if not content.endswith("\r\n"):
        content += "\r\n"

    # Clear read-only attribute defensively.
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(HOSTS_PATH))
        if attrs != 0xFFFFFFFF and (attrs & 0x1):
            ctypes.windll.kernel32.SetFileAttributesW(str(HOSTS_PATH), attrs & ~0x1)
    except Exception:
        pass

    # Write in place so we don't depend on os.replace working in System32.
    with HOSTS_PATH.open("w", encoding="utf-8", newline="") as fh:
        fh.write(content)


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


def run_once(config: dict, lock_only: bool) -> int:
    domains = config["blocked_domains"]
    timeout = float(config.get("http_timeout_seconds", 10))
    offline_policy_locked = bool(config.get("offline_is_locked", True))

    if lock_only:
        block = True
        source = "lock-only"
    else:
        url = config.get("remote_url", "")
        if not url or "YOUR_GIST" in url:
            logging.error("remote_url not configured; defaulting to locked")
            block = True
            source = "no-config"
        elif not have_network():
            block = offline_policy_locked
            source = "offline"
        else:
            state = fetch_remote_state(url, timeout)
            if state is None:
                block = offline_policy_locked
                source = "fetch-failed"
            else:
                block = interpret_state(state)
                source = f"remote({state})"

    logging.info(
        "run mode=%s identity=%s admin=%s probe=%s python=%s",
        "lock-only" if lock_only else "sync",
        whoami(), is_admin(), probe_hosts_write(), sys.executable,
    )

    try:
        changed = apply_state(block, domains)
    except PermissionError as exc:
        logging.error(
            "hosts write blocked: winerror=%s strerror=%s filename=%s probe=%s",
            getattr(exc, "winerror", None),
            getattr(exc, "strerror", None),
            getattr(exc, "filename", None),
            probe_hosts_write(),
        )
        return 6
    except Exception as exc:
        logging.exception("apply_state failed: %s", exc)
        return 7

    if changed:
        logging.info("hosts updated: blocked=%s source=%s", block, source)
        flush_dns()
    else:
        logging.info("state confirmed: blocked=%s source=%s", block, source)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help="ensure the block is in place and exit; do not contact the network",
    )
    args = parser.parse_args()

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

    return run_once(config, lock_only=args.lock_only)


if __name__ == "__main__":
    sys.exit(main())
