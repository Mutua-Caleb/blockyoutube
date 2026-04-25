"""
Push-based site blocker daemon.

Holds a long-lived SSE connection to an ntfy.sh topic. When a signed
command arrives ("block" / "unblock"), the daemon updates the Windows
hosts file in well under a second. Every 60 seconds it publishes a
heartbeat to a status topic so the Android controller can show a live
"online / locked" indicator.

Default-deny everywhere:
    - missing/invalid signature, replayed nonce, expired `until`,
      bad JSON, network failure, missing config, broker error,
      unknown command --> block stays in place.
    - the channel can ONLY unblock; it can never silently drop a block.

Stdlib only. The companion `lock.py` runs at boot as a one-shot
fallback that applies the last persisted state without contacting the
network -- so YouTube is unreachable from the moment Windows finishes
booting, even before this daemon connects.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
BEGIN_MARKER = "# BEGIN BLOCKYOUTUBE"
END_MARKER = "# END BLOCKYOUTUBE"

INSTALL_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlockYouTube"
CONFIG_PATH_DEFAULT = Path(__file__).resolve().parent / "config.json"
STATE_PATH = INSTALL_DIR / "state.json"
LOG_PATH = INSTALL_DIR / "agent.log"

HEARTBEAT_SECONDS = 60
WATCHDOG_SECONDS = 30
RECONNECT_MIN = 2
RECONNECT_MAX = 60
NTFY_DEFAULT = "https://ntfy.sh"


# ---------- platform helpers ----------------------------------------------

def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def flush_dns() -> None:
    if os.name != "nt":
        return
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


# ---------- hosts file ----------------------------------------------------

def _build_block_lines(domains: Iterable[str]) -> list[str]:
    lines = [BEGIN_MARKER]
    seen: set[str] = set()
    for raw in domains:
        d = raw.strip().lower()
        if not d or d in seen:
            continue
        seen.add(d)
        lines.append(f"0.0.0.0 {d}")
        if not d.startswith("www."):
            wd = f"www.{d}"
            if wd not in seen:
                seen.add(wd)
                lines.append(f"0.0.0.0 {wd}")
    lines.append(END_MARKER)
    return lines


def _read_hosts() -> list[str]:
    try:
        return HOSTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _strip_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    inside = False
    for line in lines:
        s = line.strip()
        if s == BEGIN_MARKER:
            inside = True
            continue
        if s == END_MARKER:
            inside = False
            continue
        if not inside:
            out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def _write_hosts(lines: list[str]) -> None:
    content = "\r\n".join(lines)
    if not content.endswith("\r\n"):
        content += "\r\n"
    if os.name == "nt":
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(HOSTS_PATH))
            if attrs != 0xFFFFFFFF and (attrs & 0x1):
                ctypes.windll.kernel32.SetFileAttributesW(str(HOSTS_PATH), attrs & ~0x1)
        except Exception:
            pass
    with HOSTS_PATH.open("w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def apply_hosts(domains: list[str]) -> bool:
    """Sync the hosts file to block exactly `domains`. [] means unblock all."""
    current = _read_hosts()
    base = _strip_block(current)
    if domains:
        desired = base + [""] + _build_block_lines(domains)
    else:
        desired = base
    if desired == current:
        return False
    _write_hosts(desired)
    return True


# ---------- state machine -------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_until(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- crypto + nonce ------------------------------------------------

def canonical_payload(cmd: dict) -> bytes:
    body = {k: v for k, v in cmd.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(secret_hex: str, cmd: dict) -> str:
    key = bytes.fromhex(secret_hex)
    return hmac.new(key, canonical_payload(cmd), hashlib.sha256).hexdigest()


def verify(secret_hex: str, cmd: dict) -> bool:
    sig = cmd.get("sig")
    if not isinstance(sig, str):
        return False
    expected = sign(secret_hex, cmd)
    return hmac.compare_digest(sig, expected)


# ---------- agent ---------------------------------------------------------

class Agent:
    def __init__(self, config: dict, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.ntfy_base = config.get("ntfy_base", NTFY_DEFAULT).rstrip("/")
        self.cmd_topic = config["cmd_topic"]
        self.status_topic = config.get("status_topic", "")
        self.secret = config["secret"]
        self.categories: dict[str, list[str]] = config.get("categories", {})
        self.default_blocked: list[str] = config.get("default_blocked", list(self.categories))
        self.stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self.state = self._load_state()

    # state persistence ----------------------------------------------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state must be an object")
            data.setdefault("mode", "block_all")
            data.setdefault("allow", [])
            data.setdefault("until", None)
            data.setdefault("last_nonce", 0)
            data.setdefault("last_message_time", 0)
            return data
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {
                "mode": "block_all",
                "allow": [],
                "until": None,
                "last_nonce": 0,
                "last_message_time": 0,
            }

    def _save_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)

    # blocklist resolution -------------------------------------------------

    def _resolve_blocklist(self) -> list[str]:
        until = parse_until(self.state.get("until"))
        if until and now_utc() >= until:
            return self._all_domains(self.default_blocked)

        mode = self.state.get("mode", "block_all")
        if mode == "block_all":
            return self._all_domains(self.default_blocked)
        if mode == "allow":
            allow = set(self.state.get("allow") or [])
            blocked_cats = [c for c in self.default_blocked if c not in allow]
            return self._all_domains(blocked_cats)
        return self._all_domains(self.default_blocked)

    def _all_domains(self, categories: Iterable[str]) -> list[str]:
        out: list[str] = []
        for cat in categories:
            out.extend(self.categories.get(cat, []))
        return out

    # apply ----------------------------------------------------------------

    def apply(self, reason: str) -> None:
        domains = self._resolve_blocklist()
        try:
            changed = apply_hosts(domains)
        except PermissionError as exc:
            logging.error("hosts write denied (%s): %s", reason, exc)
            return
        except Exception as exc:
            logging.exception("hosts write failed (%s): %s", reason, exc)
            return
        if changed:
            flush_dns()
            logging.info(
                "hosts updated reason=%s mode=%s allow=%s until=%s domains=%d",
                reason, self.state.get("mode"), self.state.get("allow"),
                self.state.get("until"), len(domains),
            )
        else:
            logging.debug("hosts unchanged reason=%s", reason)

    # command processing ---------------------------------------------------

    def handle_command(self, raw: str) -> None:
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            logging.warning("dropping non-JSON message: %r", raw[:120])
            return
        if not isinstance(cmd, dict):
            logging.warning("dropping non-object command: %r", raw[:120])
            return
        if not verify(self.secret, cmd):
            logging.warning("dropping command with bad signature: cmd=%s", cmd.get("cmd"))
            return

        nonce = cmd.get("nonce")
        if not isinstance(nonce, int):
            logging.warning("dropping command with non-int nonce")
            return
        with self._state_lock:
            if nonce <= int(self.state.get("last_nonce", 0)):
                logging.warning("dropping replayed nonce %s (last=%s)",
                                nonce, self.state.get("last_nonce"))
                return

            verb = cmd.get("cmd")
            if verb == "block":
                self.state["mode"] = "block_all"
                self.state["allow"] = []
                self.state["until"] = None
            elif verb == "unblock":
                allow = cmd.get("allow")
                if allow is None:
                    allow = list(self.default_blocked)  # full unblock
                if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
                    logging.warning("unblock with bad 'allow' field")
                    return
                until_raw = cmd.get("until")
                until_dt = parse_until(until_raw) if until_raw else None
                if until_raw and until_dt is None:
                    logging.warning("unblock with bad 'until' field: %r", until_raw)
                    return
                if until_dt and until_dt <= now_utc():
                    logging.info("unblock arrived after expiry, ignoring")
                    self.state["last_nonce"] = nonce
                    self._save_state()
                    return
                self.state["mode"] = "allow"
                self.state["allow"] = sorted(set(allow) & set(self.default_blocked))
                self.state["until"] = until_dt.isoformat().replace("+00:00", "Z") if until_dt else None
            elif verb == "ping":
                pass  # nudges a heartbeat publish, nothing else
            else:
                logging.warning("unknown command verb: %r", verb)
                return

            self.state["last_nonce"] = nonce
            self._save_state()

        self.apply(reason=f"cmd:{verb}")
        self.publish_status()

    # ntfy I/O -------------------------------------------------------------

    def _ntfy_subscribe_url(self) -> str:
        # /json streams newline-delimited JSON; easier to parse than SSE.
        params = {}
        since = int(self.state.get("last_message_time", 0))
        if since > 0:
            params["since"] = str(since)
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"{self.ntfy_base}/{urllib.parse.quote(self.cmd_topic)}/json{q}"

    def subscribe_loop(self) -> None:
        backoff = RECONNECT_MIN
        while not self.stop_event.is_set():
            url = self._ntfy_subscribe_url()
            logging.info("subscribing topic=%s since=%s",
                         self.cmd_topic, self.state.get("last_message_time"))
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "blockyoutube-agent/2.0",
                             "Accept": "application/x-ndjson"},
                )
                with urllib.request.urlopen(req, timeout=None) as resp:
                    backoff = RECONNECT_MIN  # reset on successful connect
                    for raw_line in resp:
                        if self.stop_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(evt, dict):
                            continue
                        ev = evt.get("event")
                        if ev == "keepalive" or ev == "open":
                            self.state["last_message_time"] = int(evt.get("time") or self.state["last_message_time"])
                            continue
                        if ev != "message":
                            continue
                        msg = evt.get("message", "")
                        ts = int(evt.get("time") or 0)
                        if ts:
                            self.state["last_message_time"] = ts
                            self._save_state()
                        self.handle_command(msg)
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
                if self.stop_event.is_set():
                    break
                logging.warning("subscribe disconnected: %s -- reconnecting in %ss", exc, backoff)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                logging.exception("subscribe crashed: %s", exc)
            # sleep with early exit on stop
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def publish_status(self) -> None:
        if not self.status_topic:
            return
        domains = self._resolve_blocklist()
        body = {
            "ts": int(time.time()),
            "host": socket.gethostname(),
            "mode": self.state.get("mode"),
            "allow": self.state.get("allow"),
            "until": self.state.get("until"),
            "blocked_domains": len(domains),
            "categories": list(self.default_blocked),
        }
        url = f"{self.ntfy_base}/{urllib.parse.quote(self.status_topic)}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "blockyoutube-agent/2.0",
                # nudges ntfy to keep history short for status (not a command channel)
                "Cache": "no",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            logging.debug("status publish failed: %s", exc)

    # background timers ----------------------------------------------------

    def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.publish_status()
            except Exception:
                logging.debug("heartbeat error", exc_info=True)
            self.stop_event.wait(HEARTBEAT_SECONDS)

    def watchdog_loop(self) -> None:
        """Re-assert state periodically: catches expired `until` and external hosts edits."""
        while not self.stop_event.is_set():
            try:
                # If `until` just expired, the resolver flips back to block_all and apply()
                # will rewrite hosts. Also covers the case where someone else edited hosts.
                until = parse_until(self.state.get("until"))
                if until and now_utc() >= until and self.state.get("mode") != "block_all":
                    with self._state_lock:
                        self.state["mode"] = "block_all"
                        self.state["allow"] = []
                        self.state["until"] = None
                        self._save_state()
                    logging.info("until expired, reverting to block_all")
                    self.publish_status()
                self.apply(reason="watchdog")
            except Exception:
                logging.debug("watchdog error", exc_info=True)
            self.stop_event.wait(WATCHDOG_SECONDS)

    # entry point ----------------------------------------------------------

    def run(self) -> int:
        self.apply(reason="startup")
        self.publish_status()

        threads = [
            threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self.watchdog_loop, name="watchdog", daemon=True),
            threading.Thread(target=self.subscribe_loop, name="subscribe", daemon=True),
        ]
        for t in threads:
            t.start()

        # Block until signalled. SIGBREAK on Windows, SIGTERM on POSIX, plus SIGINT.
        def _stop(*_a):
            logging.info("stop signal received")
            self.stop_event.set()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _stop)
                except (ValueError, OSError):
                    pass

        while not self.stop_event.is_set():
            self.stop_event.wait(1.0)

        return 0


# ---------- bootstrap -----------------------------------------------------

def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        str(LOG_PATH), maxBytes=512_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    for required in ("cmd_topic", "secret", "categories"):
        if required not in cfg:
            raise ValueError(f"config missing '{required}'")
    if not isinstance(cfg["categories"], dict) or not cfg["categories"]:
        raise ValueError("config 'categories' must be a non-empty object")
    try:
        bytes.fromhex(cfg["secret"])
    except ValueError:
        raise ValueError("config 'secret' must be a hex string")
    return cfg


def cmd_lock_only(config: dict) -> int:
    """Apply persisted state to hosts and exit. No network."""
    agent = Agent(config, CONFIG_PATH_DEFAULT)
    agent.apply(reason="lock-only")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BlockYouTube push-based agent")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH_DEFAULT)
    parser.add_argument("--lock-only", action="store_true",
                        help="apply persisted state and exit; do not contact the network")
    parser.add_argument("--print-key", action="store_true",
                        help="print a fresh secret + topic suggestion and exit")
    args = parser.parse_args()

    if args.print_key:
        secret = secrets.token_hex(32)
        suffix = secrets.token_urlsafe(16).replace("_", "").replace("-", "")[:16]
        print(json.dumps({
            "ntfy_base": NTFY_DEFAULT,
            "cmd_topic": f"bk-cmd-{suffix}",
            "status_topic": f"bk-stat-{suffix}",
            "secret": secret,
        }, indent=2))
        return 0

    setup_logging()

    if os.name == "nt" and not is_admin():
        logging.error("must run elevated (Administrator / SYSTEM) to edit hosts")
        return 3

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logging.error("config not found at %s", args.config)
        return 4
    except (ValueError, json.JSONDecodeError) as exc:
        logging.error("config invalid: %s", exc)
        return 4

    if args.lock_only:
        return cmd_lock_only(config)

    agent = Agent(config, args.config)
    try:
        return agent.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
