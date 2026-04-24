# BlockYouTube

A simple Windows website blocker with **remote unlock from Android**. Sites
stay blocked on every boot; to unlock you flip one field in a JSON file
hosted on a GitHub Gist (editable from the GitHub Android app, any phone
browser, or a Tasker/HTTP-client shortcut). The Windows client works on
**any network** because it just polls an HTTPS URL.

## How it works

```
 Android (edits gist) ──► GitHub Gist ──► (polled every 5 min) ──► Windows client ──► hosts file
```

- **Default-deny.** The hosts-file block is written during install, so
  YouTube is unreachable from the moment install finishes — before any
  scheduled task ever runs.
- **Two short-lived Scheduled Tasks** run as **SYSTEM**:
  - `BlockYouTube-Lock` — one-shot at boot, `--lock-only`, no network.
    Re-asserts the block in case anything edited the hosts file.
  - `BlockYouTube-Sync` — one-shot every 5 minutes. Polls the gist; if
    unlocked, removes the block. If locked, ensures it's there.
- Each task invocation finishes in seconds. There is no long-running
  process to die. If the sync task ever stops working, you stay
  blocked — the only thing it can do is *unlock*.
- Entries are bracketed by `# BEGIN BLOCKYOUTUBE` / `# END BLOCKYOUTUBE`
  so the script never touches the rest of your hosts file.
- If the network is down, the sync task defaults to **locked**.

## One-time setup

### 1. Create the remote state file (GitHub Gist)

1. Go to <https://gist.github.com> → **New gist**.
2. Filename: `state.json`. Contents:
   ```json
   { "locked": true }
   ```
3. Create it as a **secret gist** (the URL is unguessable; nobody can
   edit it without your GitHub login, and the Windows client only needs
   to read it).
4. Click **Raw** and copy the URL. It will look like:
   `https://gist.githubusercontent.com/<user>/<id>/raw/state.json`

### 2. Install Python on the Windows machine

Install Python 3.10+ from <https://www.python.org/downloads/windows/>.
When installing:

- Check **Add python.exe to PATH**
- Choose **Install for all users** (so the SYSTEM account finds it too)

No extra packages are required — the blocker uses only the standard
library.

### 3. Configure and install

1. Copy this folder to the Windows machine.
2. Edit `config.json` and paste your Gist raw URL into `remote_url`.
   Add or remove domains in `blocked_domains` as you like.
3. Open **PowerShell as Administrator** in this folder and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   This copies the files to `C:\ProgramData\BlockYouTube\`, writes the
   block to the hosts file immediately, and registers two SYSTEM tasks:
   `BlockYouTube-Lock` (boot) and `BlockYouTube-Sync` (every 5 min).

4. Verify — try visiting `youtube.com` immediately, it should already be
   blocked. To inspect:
   ```powershell
   Get-ScheduledTask BlockYouTube-Lock, BlockYouTube-Sync
   Get-Content "$env:ProgramData\BlockYouTube\blocker.log" -Tail 20
   ```

## Unlocking from Android

### Option A — GitHub Android app (simplest)

1. Install the **GitHub** app, sign in.
2. Open your gist, tap the pencil icon on `state.json`.
3. Change `"locked": true` to `"locked": false`, save.
4. Within ~5 minutes the Windows machine clears the hosts entries.
   (To force an immediate sync from the Windows side:
   `Start-ScheduledTask -TaskName BlockYouTube-Sync` from any PowerShell.)

### Option B — Timed unlock (auto-relock)

Edit `state.json` to:

```json
{ "locked": false, "until": "2026-04-24T22:00:00Z" }
```

`until` is a UTC timestamp. When it passes the client treats the state
as locked again, even if you forget to change the gist back. Great for
"give me 1 hour" windows.

### Option C — HTTP shortcut (fastest, one tap)

Create a personal access token at <https://github.com/settings/tokens>
with **gist** scope only. In any HTTP client on your phone (HTTP
Shortcuts, Tasker, etc.) create a request:

- Method: `PATCH`
- URL: `https://api.github.com/gists/<YOUR_GIST_ID>`
- Headers:
  - `Authorization: Bearer <YOUR_TOKEN>`
  - `Accept: application/vnd.github+json`
- Body (unlock for 1 hour — recompute `until` in the shortcut if you can,
  or just flip the flag):
  ```json
  {"files":{"state.json":{"content":"{\"locked\": false}"}}}
  ```

Make a second shortcut with `"locked": true` to relock on demand.

> Keep the token on-device only. Treat it like a password — anyone with
> it and the gist URL can toggle your lock.

## Uninstall

From an elevated PowerShell in this folder:
```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```
Removes the task and scrubs the `BLOCKYOUTUBE` section of the hosts file.

## Files

| File | Purpose |
| --- | --- |
| `blocker.py` | The polling daemon (stdlib only). |
| `config.json` | Remote URL, poll interval, blocked domain list. |
| `install.ps1` | Copies files, registers the boot-time Scheduled Task. |
| `uninstall.ps1` | Removes the task and cleans the hosts file. |
| `state.example.json` | Example contents for your Gist. |

## Caveats and threat model

- This is a soft block. Anyone with local admin can disable the task or
  edit the hosts file. It's aimed at resisting your own impulses, not an
  adversary on your machine.
- Apps that use DNS-over-HTTPS (e.g. Chrome with DoH enabled) can bypass
  the hosts file. Disable DoH in the browser, or push the blocklist
  lower (e.g. via a hosts-level tool like a Windows firewall rule) if
  you need stronger enforcement.
- GitHub's raw-gist CDN sometimes serves cached content for ~60s; the
  client adds a cache-buster query string, but unlocks may still take a
  poll cycle to take effect. Increase `poll_seconds` in `config.json`
  if you want to be gentler on GitHub's rate limits.
