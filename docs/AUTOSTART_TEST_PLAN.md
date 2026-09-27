# Autostart test plan (PS-34a)

Proves that the `PhotonScript` scheduled task starts PhotonScript by itself,
in jeremy's desktop session, after an install, a sign-in, a crash and a
reboot, and that it stays healthy for a whole night.

The tool behind every step is `photonscript autostart-check`. It is read-only
(GET requests, the task definition, two Winlogon values, the process list and
the logs) and prints one `PASS` / `WARN` / `FAIL` line per check, then a
verdict. Exit code 1 means at least one FAIL. The only option that changes
anything is `--kill`, which hard-kills the service for the crash test and
refuses while the armer is ARMED, RUNNING or PAUSED_UNSAFE.

## Ground rules

- Daytime only, nothing RUNNING: armer DISARMED (the noon auto-arm is off).
  Check before every step (scope PC, normal PowerShell):

  ```powershell
  (Invoke-RestMethod http://localhost:8100/api/arm).state
  ```

  Expect `DISARMED` (or `COMPLETE` after a dawn shutdown). If it says ARMED,
  RUNNING or PAUSED_UNSAFE, stop here.
- "Normal PowerShell" = not "Run as administrator". Only the installer needs
  an elevated one. Never start PhotonScript itself from an elevated shell.
- Paths: repo `C:\astro\PhotonScript`, data_dir `C:\Users\jeremy\.photonscript`,
  scope API `http://100.94.189.77:8100` from the desktop.
- `SCOPE-PC` in the expected output stands for the scope PC's computer name.

## What `autostart-check` checks

| Line | PASS means | If it FAILs or WARNs |
|---|---|---|
| `task.exists` | task `PhotonScript` is registered | FAIL: run step (a). WARN "could not read": run the check from an elevated PowerShell (still read-only) |
| `task.state` | enabled and Running | Disabled: `Enable-ScheduledTask PhotonScript` (elevated). Ready: `Start-ScheduledTask PhotonScript` |
| `task.logon` | Interactive as jeremy | S4U/Password: re-run step (a) without `-LogonType` |
| `task.trigger` | at log on of jeremy, delay PT30S | re-run step (a) |
| `task.action` | powershell runs `C:\astro\PhotonScript\deploy\run-photonscript.ps1 -Launcher task-Interactive` | re-run step (a) from the right checkout |
| `task.settings` | no time limit, priority 4, one instance | re-run step (a) |
| `task.last_run` | `0x41301 running` | `0x0`: the wrapper exited (read `wrapper.log`, `supervisor.log`); `0xC000013A`: the session closed (sign-out); anything else: read the logs, then `Start-ScheduledTask PhotonScript` |
| `autologon` | AutoAdminLogon=1 for jeremy (the password is never read) | set up auto-logon (step a.3); without it nothing starts after a reboot until someone signs in |
| `api.health` | `/api/health` answered in under 1 s | refused: not running (`Get-Content C:\Users\jeremy\.photonscript\logs\supervisor.log -Tail 30`). No answer / SLOW: stalled, read `stalls.log` |
| `service.launcher` | `task-Interactive` | `console` or `unknown`: a hand-started copy is running, not the task. `photonscript stop`, then `Start-ScheduledTask PhotonScript` |
| `service.session` | session id not 0 | session 0 means the S4U environment from PS-55: re-run step (a) |
| `service.priority`, `service.throttling` | normal, power throttling off | the PS-55 QoS guard did not apply: send the `Process:` line from `photonscript.log` to Claude |
| `service.commit` | running SHA = `git rev-parse HEAD` of the repo | WARN only: `photonscript restart` runs what is on disk |
| `loop.lag` | lag under 250 ms, max under 2 s in 5 min, no stalls | WARN in the first 10 min is usually the startup spike: run again. FAIL: read `stalls.log` |
| `process.wrapper` / `.supervisor` / `.service` | exactly one of each | none: not running. Two: a console copy runs beside the task; close it with `photonscript stop` and Start-ScheduledTask |
| `process.launched_by` | the wrapper carries `-Launcher task-...` | a console wrapper is running: close it (see above) |
| `process.api_pid` | `/api/health` is served by the supervised service | something else holds port 8100: find it with `Get-NetTCPConnection -LocalPort 8100` |
| `hold` | (only shown when set) | `photonscript stop` left a HOLD: `Start-ScheduledTask PhotonScript` clears it |
| `log.stalls` | no `STALLED` entry in the last `--hours` (default 24) | FAIL = stall since this start: send `stalls.log` to Claude |
| `log.supervisor` | no crash restarts in the window | WARN is expected right after the crash test; FAIL = crash-loop give-up |
| `nina.1`, `nina.2`, `phd2` | NINA #1 (1888), NINA #2 (1889), PHD2 (4400) answer | WARN only: PhotonScript does not start them; start them before arming |
| `tailscale` | `https://teles-feb25.lobster-bleak.ts.net/api/health` answers | WARN only: `tailscale serve status` |

## 0. Ship the checker (desktop)

```powershell
cd C:\dev\PhotonScript
git status --short
git switch main
git merge ps-34a-autostart-selftest
.\deploy\deploy.ps1
```

Expected: tests pass, push, and deploy.ps1 reports the scope's `/api/health`
on the new SHA. On failure: stop and send the output to Claude; nothing on
the scope PC has changed.

## (a) Install the task

1. Scope PC, normal PowerShell. Stop the copy that runs in the console window
   today (it is not under the task: `/api/health` says launcher `unknown`):

   ```powershell
   photonscript stop
   Start-Sleep -Seconds 10
   photonscript status
   ```

   Expected: "Signaled pid N to shut down gracefully", then status shows
   `Process: not running` and `Hold: HOLD set`, and the console window says
   `supervisor exited with code 0; wrapper done`. If the process is still
   running after 30 s: `photonscript stop --force`.

2. Scope PC, **elevated** PowerShell (Run as administrator):

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-autostart.ps1 -StartNow
   ```

   Expected:

   ```
   Registered scheduled task 'PhotonScript' (runs as SCOPE-PC\jeremy, logon type Interactive).
   State: Running  Last result: 0x41301
   ```

   A yellow "Note: Windows auto-logon for 'jeremy' is not set" line means
   step 3 is needed. On a red error: send it to Claude and use (h) to run
   from the console tonight.

3. Only if the installer printed the auto-logon note. Check what is set
   (reads two values, never the password):

   ```powershell
   Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' | Select-Object AutoAdminLogon, DefaultUserName, DefaultDomainName, AutoLogonCount
   ```

   To turn it on, run Sysinternals Autologon (`Autologon64.exe`, from
   learn.microsoft.com/sysinternals/downloads/autologon), enter `jeremy`,
   the computer name as the domain and the password, click Enable. It stores
   the password encrypted (an LSA secret) rather than as plain text in the
   registry. Expected afterwards: `AutoAdminLogon 1`, `DefaultUserName jeremy`,
   and no `AutoLogonCount`.

## (b) Run the checker

Scope PC, normal PowerShell, at least 60 s after (a):

```powershell
photonscript autostart-check
```

Expected (NINA/PHD2 lines are WARN if they are not open, which is fine in
daytime):

```
PASS  task.exists          'PhotonScript' is registered
PASS  task.state           enabled, Running
PASS  task.logon           Interactive as SCOPE-PC\jeremy
PASS  task.trigger         at log on of SCOPE-PC\jeremy, delay PT30S
PASS  task.action          powershell.exe -NoProfile ... run-photonscript.ps1" -Launcher task-Interactive
PASS  task.settings        no time limit, priority 4, one instance
PASS  task.last_run        0x41301 running; last run ...; next at next logon
PASS  autologon            AutoAdminLogon=1 for SCOPE-PC\jeremy (password not read; ...)
PASS  api.health           answered in 40 ms
INFO  service              pid 1234, mode full, version ..., up 1m 5s, armer DISARMED
PASS  service.launcher     task-Interactive
PASS  service.session      session 1 (a desktop session)
PASS  service.user         jeremy
PASS  service.priority     normal
PASS  service.throttling   power throttling off
PASS  service.commit       abc1234 = HEAD of C:\astro\PhotonScript
PASS  loop.lag             now 15 ms, max 400 ms in 5 min, 0 stall(s) in 5 min, ...
PASS  process.wrapper      one run-photonscript.ps1 wrapper (pid ...)
PASS  process.supervisor   one photonscript supervise (pid ...)
PASS  process.service      one photonscript start (pid ...)
PASS  process.launched_by  the wrapper was started by the scheduled task
PASS  process.api_pid      /api/health pid 1234 is the supervised service
PASS  log.stalls           no stalls in the last 24 h
PASS  log.supervisor       last 24 h: 2 start(s), 0 crash restart(s)
WARN  nina.1               http://localhost:1888/v2/api not reachable ...
PASS  tailscale            https://teles-feb25.lobster-bleak.ts.net: HTTP 200 in 60 ms
AUTOSTART OK with warnings: 1 warn, 24 pass
```

Pass: exit code 0 (`$LASTEXITCODE`), no FAIL line. On a FAIL: use the table
above; a `loop.lag` WARN in the first 10 minutes is the startup spike, run
it again after 10 minutes.

## (c) Sign out and back in

Tests that the "at log on" trigger fires. Scope PC:

1. Note the pid: `(Invoke-RestMethod http://localhost:8100/api/health).pid`
2. Start menu, jeremy, **Sign out** (not Disconnect). Everything in the
   session stops, PhotonScript included.
3. From the desktop, confirm it is down:

   ```powershell
   try { Invoke-RestMethod http://100.94.189.77:8100/api/health -TimeoutSec 5 } catch { "down: $($_.Exception.Message)" }
   ```

   Expected: `down: ...` (connection refused or unable to connect). If
   Windows auto-logon signs jeremy straight back in, it comes back by itself
   within 2 minutes; that also passes, skip the sign-in and wait 2 minutes
   before step 5.
4. Sign in again as jeremy (at the console or over Remote Desktop; both count
   as a log on). Note the time.
5. After 2 minutes, scope PC normal PowerShell:

   ```powershell
   photonscript autostart-check
   Get-Content C:\Users\jeremy\.photonscript\logs\wrapper.log -Tail 3
   ```

   Expected: verdict OK, a new pid, `wrapper start (... launcher task-Interactive)`
   about 30 s after the sign-in, healthy within 45 to 90 s of the sign-in.
   `task.last_run` may show `0xC000013A` for a moment if the check runs
   before the new start; it becomes `0x41301 running`.

On failure (nothing after 3 minutes): `Get-ScheduledTask PhotonScript | Get-ScheduledTaskInfo`
and the task's History tab in Task Scheduler (Event Viewer, Microsoft,
Windows, TaskScheduler, Operational). Start it by hand with
`Start-ScheduledTask PhotonScript` and send the output to Claude.

## (d) Crash test with Pushover

Tests that the supervisor restarts a dead service and alerts. Scope PC,
normal PowerShell, armer DISARMED:

```powershell
photonscript autostart-check --watch-restart --kill
```

It runs the checks, kills the service pid (only the service, not the
supervisor) and times the recovery. Expected tail:

```
Service pid 1234, armer DISARMED.
Killed pid 1234 (...).
Old service is gone; waiting for the supervisor to start a new one.
PASS  watch.recover        new pid 5678 healthy 14 s after the kill (expect about 10 to 40 s: ...)
PASS  watch.supervisor     PhotonScript exited with code 1 after 12 min -> crash
PASS  watch.pushover       'PhotonScript crashed' was sent: check the phone
```

and the phone gets "PhotonScript crashed: PhotonScript on SCOPE-PC exited with
code 1 after N min. Restarting in 5 s (crash 1 of 5 allowed in 15 min)."

If you prefer to pull the trigger yourself, leave out `--kill`: it prints the
pid and waits up to 5 minutes; in a second PowerShell run
`Stop-Process -Id <pid> -Force`.

Notes and failures:
- `watch.pushover` WARN "quiet-daytime": in daylight each alert title goes
  out at most once per 4 h (PS-50), so a second crash test the same day is
  logged in `notifications.jsonl` but not pushed. Not a failure.
- Do not repeat the kill more than 3 times in 15 minutes: 5 crashes in 15
  minutes and the supervisor gives up ("PhotonScript is down" alert) and the
  task ends. Recover with `Start-ScheduledTask PhotonScript`.
- `watch.recover` FAIL: `Get-Content C:\Users\jeremy\.photonscript\logs\supervisor.log -Tail 30`,
  then `Start-ScheduledTask PhotonScript` and send the log to Claude.

## (e) Full reboot

Tests auto-logon plus the trigger, end to end. Note which programs must be
reopened afterwards (NINA x2, PHD2): PhotonScript does not start them.

1. Desktop PowerShell, start a watcher that prints every 10 s (leave it
   running):

   ```powershell
   $t0 = Get-Date; $wasDown = $false
   while ($true) {
     $h = $null; try { $h = Invoke-RestMethod http://100.94.189.77:8100/api/health -TimeoutSec 5 } catch { }
     $el = ((Get-Date) - $t0).ToString("mm\:ss")
     if ($h) { "$el UP pid $($h.pid) launcher $($h.process.launcher) up $([int]$h.uptime_s)s" } else { "$el down"; $wasDown = $true }
     if ($h -and $wasDown) { "RECOVERED after $el"; break }
     Start-Sleep -Seconds 10
   }
   ```

2. Scope PC: Start menu, Power, **Restart** (or `Restart-Computer` in a
   normal PowerShell).
3. Expected on the desktop: `down` lines while Windows restarts, then `UP`
   with a new pid and `launcher task-Interactive`. Typical: 1 to 3 minutes
   to the desktop (auto-logon), plus 30 s trigger delay, plus 10 to 30 s to
   healthy, so **RECOVERED within 5 minutes**. More than 10 minutes is a
   failure.
4. Scope PC (Remote Desktop in; that takes over the auto-logon session, it
   does not start a second one), normal PowerShell:

   ```powershell
   photonscript autostart-check
   ```

   Expected: verdict OK; `service.session` not 0; NINA/PHD2 WARN until you
   open them.

On failure:
- Stuck at the sign-in screen: auto-logon is not working; redo (a.3).
- Signed in but never UP: `Get-ScheduledTask PhotonScript | Get-ScheduledTaskInfo`
  and `Get-Content C:\Users\jeremy\.photonscript\logs\wrapper.log -Tail 10`;
  start it with `Start-ScheduledTask PhotonScript`.

## (f) Remote Desktop: disconnect vs sign out

- **Disconnect** (close the Remote Desktop window, or Start, jeremy,
  Disconnect): the session keeps running, and so does PhotonScript. From
  the desktop:

  ```powershell
  Invoke-RestMethod http://100.94.189.77:8100/api/health | Select-Object pid, uptime_s, @{n='launcher';e={$_.process.launcher}}
  ```

  Expected: the same pid as before you disconnected, uptime still growing.
  Repeat after 5 minutes. A new pid means it restarted: read
  `supervisor.log`.
- **Sign out** stops PhotonScript (and NINA/PHD2) until the next sign-in or
  reboot; that is what (c) tests. Always disconnect, never sign out, when
  leaving the scope PC for the night.

## (g) One full night under the task

Arm as usual. The next morning, scope PC normal PowerShell:

```powershell
photonscript autostart-check --hours 16
(Select-String -Path C:\Users\jeremy\.photonscript\logs\stalls.log -Pattern "STALLED for .* \(new\)" -ErrorAction SilentlyContinue | Measure-Object).Count
(Select-String -Path C:\Users\jeremy\.photonscript\logs\photonscript.log* -Pattern "Event loop lag" | Measure-Object).Count
Select-String -Path C:\Users\jeremy\.photonscript\logs\supervisor.log -Pattern "-> crash|Giving up|Started PhotonScript" | Select-Object -Last 10
```

Pass: verdict OK, `log.stalls` PASS (0 stalls in 16 h), `log.supervisor`
0 crash restarts, `Event loop lag` count small (single digits), the same pid
all night (uptime from `/api/health` covers the night), and a normal frame
count in the morning report. Then PS-34a moves to Done.

On failure: send `stalls.log`, the last 200 lines of `supervisor.log` and
`photonscript autostart-check --json --hours 16` to Claude. For the next
night, roll back (h).

## (h) Rollback: run from the console again

1. Scope PC, normal PowerShell, stop the service **first** (stopping the
   task alone kills only the PowerShell wrapper and can leave the supervisor
   and service running as orphans):

   ```powershell
   photonscript stop
   Start-Sleep -Seconds 10
   photonscript status
   ```

   Expected: `Process: not running`, `Supervisor: not running`.
2. Elevated PowerShell, remove the task (or keep it but switch it off with
   `Disable-ScheduledTask PhotonScript`):

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\install-autostart.ps1 -Uninstall
   ```

   Expected: `Removed scheduled task 'PhotonScript'.`
3. Normal PowerShell, start the console wrapper and leave the window open:

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\run-photonscript.ps1
   ```

   Expected: `wrapper start (... launcher console)` and, after 30 s,
   `photonscript status` in another window shows the API up.
   `photonscript autostart-check` now FAILs the task and launcher lines;
   that is expected while rolled back.
4. To go back to the task later: `photonscript stop`, then step (a).
