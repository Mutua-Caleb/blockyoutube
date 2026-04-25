# BlockYouTube

Push-based site blocker for Windows, controlled remotely from your phone
across **any network**, signed with HMAC so the channel can't be hijacked.
Locks YouTube + a configurable list of social-media domains via the hosts
file. Default-deny on every failure path — the remote channel can only
ever *unblock*; it can never silently lose a block.

## Architecture

```
  ┌──────────────┐    POST signed JSON    ┌───────────┐    SSE stream    ┌──────────────────┐
  │  Android PWA │ ─────────────────────► │  ntfy.sh  │ ───────────────► │  Windows agent   │
  │ (controller/)│                        │ (broker)  │                  │   (agent.py)     │
  │              │ ◄──────EventSource──── │           │ ◄──── POST ───── │ writes hosts +   │
  └──────────────┘   live status topic    └───────────┘  heartbeat 60s   │ heartbeats       │
                                                                          └──────────────────┘
```

- **Broker:** [`ntfy.sh`](https://ntfy.sh) — free, public, pub/sub over HTTP.
  No account, no API keys. Self-hostable when you want to. Two random topic
  names act as the channel; one for commands, one for status.
- **Authentication:** Every command is HMAC-SHA256 signed with a 32-byte
  secret. The agent rejects bad signatures, replayed nonces, malformed
  payloads, or `until` timestamps already in the past. Topic-name leak
  alone gets an attacker nothing.
- **Push, not poll:** Windows agent holds a long-lived HTTP stream
  (`/json` ndjson) to the cmd topic. Commands land in <1s, not 5 min.
- **Live status:** Agent publishes `{mode, allow, until, host, ts}` to
  the status topic every 60s. The PWA's traffic-light dot turns green/
  yellow/red based on heartbeat age, so you always know whether the
  agent is actually reachable.
- **Default-deny on every error:** missing config, bad signature, expired
  `until`, network drop, broker outage, hosts-write failure, unknown
  command verb — all collapse to "blocked". The agent is also restarted
  by the OS on crash, with a one-shot boot lock task that re-applies the
  last persisted state before the agent has reconnected.

## Components

| Path                  | Purpose                                                 |
| --------------------- | ------------------------------------------------------- |
| `agent.py`            | Long-lived Windows daemon (subscribe + apply + status). |
| `keygen.py`           | Generates a fresh secret + topic pair into config.json. |
| `cli.py`              | Cross-platform CLI controller for testing / scripting.  |
| `config.json`         | Topics, secret, categories, default-blocked list.       |
| `install.ps1`         | Registers two SYSTEM scheduled tasks.                   |
| `uninstall.ps1`       | Removes tasks, cleans hosts, kills lingering agent.     |
| `controller/`         | Static PWA — Android home-screen controller.            |

## One-time setup

### 1. Generate a topic + secret on the Windows machine

```powershell
cd <this folder>
python keygen.py --write config.json
```

This writes a fresh `cmd_topic`, `status_topic` and `secret` into
`config.json` (preserving your category lists if any). It also prints
the controller-side snippet to stdout — copy that block, you'll paste it
into the phone in step 4.

### 2. Install Python 3.10+ on the Windows machine

From <https://www.python.org/downloads/windows/>:

- Check **Add python.exe to PATH**
- **Install for all users** (so the SYSTEM account can find it)

No third-party packages required — agent uses only the standard library.

### 3. Run the installer

In an **elevated PowerShell**, in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

This:

- Copies `agent.py` + `config.json` to `C:\ProgramData\BlockYouTube\`.
- Writes the hosts-file block immediately (default-deny).
- Registers two scheduled tasks running as `SYSTEM`:
  - `BlockYouTube-Lock` — boot one-shot, `--lock-only`, no network.
  - `BlockYouTube-Agent` — long-lived push subscriber. Restarts on crash.
- Starts the agent right away so you can test without rebooting.

Verify:

```powershell
Get-ScheduledTask BlockYouTube-Lock, BlockYouTube-Agent
Get-Content "$env:ProgramData\BlockYouTube\agent.log" -Tail 30 -Wait
```

### 4. Set up the phone (PWA)

Host `controller/` somewhere your phone can reach — easiest options:

- **GitHub Pages** — push `controller/` to a public repo, enable Pages.
  The PWA does no server-side anything; it's pure static HTML/JS.
- **Localhost on the phone** — copy `controller/` to your phone and run
  any static file server (e.g. Termux + `python -m http.server`).
- **Any static host** — Netlify, Vercel, Cloudflare Pages, S3.

> The PWA holds your secret in `localStorage`. It does not need to
> phone home anywhere except `ntfy.sh`. Ideally serve it over HTTPS
> from a host you trust.

On your Android phone:

1. Open the page in Chrome.
2. **Add to Home screen** (the page is a PWA — installable, works offline).
3. On first launch, paste the JSON snippet `keygen.py` printed in step 1
   into the setup field. Tap **Save**.
4. The status indicator should turn green within ~60s, showing the
   current mode (`locked` / `unlocked …`) and host name.

## Using it

- **Block everything**: red button. Sets `mode=block_all`.
- **Unblock for 15 min / 30 min / 1 h / 3 h / no limit**: quick buttons.
  If no categories are checked it unblocks all of them; otherwise it
  unblocks only the ones you checked.
- **Apply allowlist**: same idea but with no automatic relock.
- **Ping**: nudges the agent to publish a fresh heartbeat (handy when
  testing).

The traffic-light indicator at the top is your liveness signal:

| Color   | Meaning                                          |
| ------- | ------------------------------------------------ |
| Green   | Heartbeat <90s old. Agent reachable. State shown. |
| Yellow  | Heartbeat 90s–5min old. Agent likely fine, slow. |
| Red     | No heartbeat for 5+ min. Agent unreachable.      |
| Grey    | No heartbeat received yet (just opened).         |

## Using the CLI controller

For desktop testing, scripting, or as a fallback when the phone is
dead:

```bash
# Make a controller-only config (the four fields the PWA also needs).
python keygen.py > controller-config.json   # only on a fresh install

python cli.py --config controller-config.json status
python cli.py --config controller-config.json block
python cli.py --config controller-config.json unblock --for 30m
python cli.py --config controller-config.json unblock --only youtube --until 2026-04-25T22:00Z
```

## Customising the blocklist

Edit `categories` and `default_blocked` in `config.json`. Re-run
`install.ps1` (or just copy the file to `C:\ProgramData\BlockYouTube\`)
— the agent picks up changes the next time it restarts. The PWA's
allowlist checkboxes are wired to a hard-coded list in `controller/app.js`;
add any new category names there too.

## Threat model

- **Soft block.** Anyone with local admin can stop the task or edit
  the hosts file. This is for resisting your own impulses, not an
  adversary on your machine.
- **DNS-over-HTTPS bypasses the hosts file.** Disable DoH in your
  browser, or push the blocklist down a layer (Pi-hole, NextDNS,
  router). The hosts-file approach catches every app that uses the
  system resolver — most do.
- **Topic name + secret should be treated as a password.** Anyone
  with both can unblock. Topic name alone is useless without the
  secret (HMAC). Secret alone is useless without the topic name.
- **`ntfy.sh` operator can see traffic.** They see the topic names
  and the JSON payloads (signatures + commands). They can't forge
  commands without the secret. If you don't want a third party seeing
  your social-media usage pattern, self-host ntfy and point
  `ntfy_base` at it.

## Uninstall

From an elevated PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

Removes the tasks, kills any lingering agent process, and scrubs the
`BLOCKYOUTUBE` section from the hosts file.
