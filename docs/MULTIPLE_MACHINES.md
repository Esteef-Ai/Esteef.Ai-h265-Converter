# Using more than one PC

One PC, the **main PC**, owns the queue and the dashboard. Other PCs, called **workers**, ask it for the next file, encode and verify it with the same settings, replace it on the shared drive, and report back. The main PC keeps converting too.

Each file goes to one PC at a time. If a worker goes quiet for 15 minutes (crash, sleep, powered off), its file goes back in the queue.

## Before you start

**The files must be on a network share that every PC can open.** Workers only take files they can reach this way:

- On the main PC, add folders through a **mapped network drive** (for example `V:\Videos` mapped to `\\nas\media`). The app translates this for each worker, so workers don't need the same drive letter.
- Files on the main PC's own disk are converted only by the main PC, unless you share that disk and set up `worker_path_map` (see [Other setups](#other-setups)).

**On each worker,** open the share in File Explorer once (for example `\\nas\media`) and save the login if asked. The worker runs under your Windows account and uses the same access.

Each worker also needs Windows 10 or 11 and enough free disk space on C: to hold one file at a time, plus 15 GB. The installer sets up the rest. If Python is missing, it installs Python 3.13 for the current user with `winget`.

> **Security warning.** Enabling workers makes the dashboard reachable by every device on your network, and **it has no login**. Anyone who can reach it can browse folder names on the main PC, add folders to convert, and permanently delete quarantined originals. The worker key also travels unencrypted. Do this only on a private network you trust, and never expose the port to the internet.

## 1. Set up the main PC

Finish the [normal setup](../README.md#setup) first. Then, in the app folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -EnableWorkers
```

This does three things:

- creates a random worker key and saves it in `config.json`
- sets the dashboard to accept connections from your network
- prints the two commands used in the next steps

Restart the app so the change takes effect: double-click `Stop AIConverter.bat`, then `Start AIConverter.bat`.

Run the printed firewall command once, in a PowerShell window opened **as administrator**. It looks like this:

```powershell
New-NetFirewallRule -DisplayName AIConverter -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -RemoteAddress LocalSubnet
```

## 2. Install each worker

On each worker PC, open a normal PowerShell window and paste the install command printed in step 1. It looks like this, with your own address and key:

```powershell
powershell -ExecutionPolicy Bypass -Command "iwr 'http://<main-pc-ip>:8765/worker/setup.ps1?key=<your-key>' -UseBasicParsing | iex"
```

It needs no administrator rights. It installs to `C:\AIConverterWorker`, copies ffmpeg over from the main PC (about 250 MB), and adds desktop shortcuts.

Treat that command like a password, because it contains your worker key.

## 3. Start helping

Double-click **AIConverter Worker - Start** on the worker's desktop. **A worker never starts by itself**, so the PC stays yours until you choose to lend it. Within a few seconds it should appear in the **Computers** panel on the main PC's dashboard, and then pick up a file.

## Controlling a worker

Use the desktop shortcuts, the control page at http://127.0.0.1:8766 on the worker, or the Computers panel on the main dashboard.

| Control | What it does |
|---|---|
| **Pause now** | Freezes the encode instantly and keeps its place. |
| **Pause after this file** | Finishes the current file, then takes no more. |
| **Resume** | Carries on. A paused worker stays paused across restarts until you resume it. |
| **Stop file and return to queue** | Gives up the current file so another PC can take it, then pauses. Work done on that file is lost. |
| **Quit** | Hands back the current file and shuts the worker down. |

Encoding runs at below-normal priority on workers, so it gives way to whatever else you're doing on that PC. The worker also keeps the PC awake while it's working.

## Updating a worker

When you update the main PC, run the same install command on each worker. It replaces the code and keeps the worker's settings and pause state.

## Troubleshooting

| What you see | What to check |
|---|---|
| The worker doesn't appear on the dashboard | From the worker, open `http://<main-pc-ip>:8765` in a browser. If that fails, check the firewall rule, that `web_host` is `0.0.0.0` in the main PC's `config.json`, and that the app was restarted. |
| The worker's log says "master rejected the worker key" | The key changed. Run the install command again. |
| The worker's log says "Cannot reach..." | The worker can't open the share. Open it in File Explorer on that PC and save the login. |
| The worker is connected but never takes a file | The queued files may be on the main PC's own disk rather than a share, or the queue may be empty. |
| Files sit waiting after a share outage | That's normal. Jobs wait for the share to come back rather than failing. |

The worker's log is at `C:\AIConverterWorker\logs\aiconverter.log`. The main PC's is `logs\aiconverter.log` in its app folder.

## Other setups

**Folders added by UNC path, or local disks you've shared.** Automatic translation covers mapped network drives only. For anything else, tell the main PC how a worker should reach each location, in `config.json`:

```json
"worker_path_map": { "D:\\Videos\\": "\\\\main-pc\\videos\\" }
```

**Changing the port.** Set `web_port` in the main PC's `config.json`, then update the firewall rule and reinstall workers using the new address.
