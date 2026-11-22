# Scheduling the daily run (Windows Task Scheduler + WSL)

Nothing in this repository schedules itself. Follow these steps only when you
have decided to turn the daily run on.

## 0. Before scheduling anything

```bash
# 1. Rehearse with no writes and no model calls.
uv run job-agent daily

# 2. Rehearse with model calls, still no writes. Check the scores look sane.
uv run job-agent daily --ai

# 3. Rehearse the exact scheduled command, still without writes.
DRY_RUN=1 ./scripts/run_daily.sh && tail -40 logs/daily-$(date +%Y-%m).log

# 4. Only then allow the real thing once, by hand, and read the result.
./scripts/run_daily.sh && tail -40 logs/daily-$(date +%Y-%m).log
```

The OAuth sign-in needs a browser once. Do that interactively (`uv run
job-agent check`) before scheduling, so the scheduled task only ever refreshes
an existing `token.json`.

## 1. Windows Task Scheduler wrapper

Create `C:\Users\<you>\job-agent-daily.cmd`:

```bat
@echo off
rem Replace Ubuntu with your distro name from `wsl -l -v`.
rem -lc runs a login shell so uv is on PATH.
wsl.exe -d Ubuntu -- bash -lc "cd ~/job-application-agent && ./scripts/run_daily.sh"
exit /b %ERRORLEVEL%
```

Check it by double-clicking it once, then reading
`\\wsl$\Ubuntu\home\<you>\job-application-agent\logs\`.

## 2. Register the task

GUI: Task Scheduler → Create Task.

- **General**: name `Job Agent Daily`. "Run only when user is logged on"
  (the task needs your WSL user's `token.json` and `.env`). Do **not** tick
  "Run with highest privileges" — it is not needed.
- **Triggers**: Daily, once, e.g. 07:30. Tick "Stop task if it runs longer
  than 1 hour".
- **Actions**: Start a program → `C:\Users\<you>\job-agent-daily.cmd`.
- **Conditions**: tick "Start only if the following network connection is
  available: Any connection". Untick "Stop if the computer switches to battery
  power" only if you want it to run unplugged.
- **Settings**: "If the task fails, restart every 30 minutes, up to 2 times."
  Leave "Run a new instance in parallel" **off** — the pipeline is a single
  writer. `flock` in `run_daily.sh` is the backstop, not the primary guard.

Equivalent one-liner, run in an elevated PowerShell:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\$env:USERNAME\job-agent-daily.cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At 7:30am
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "Job Agent Daily" -Action $action `
            -Trigger $trigger -Settings $settings -Description `
            "Discovers and scores jobs. Never submits an application."
```

`-MultipleInstances IgnoreNew` is the Task Scheduler equivalent of the single
writer rule. `-StartWhenAvailable` catches up after the machine was asleep.

To check or remove it:

```powershell
Get-ScheduledTaskInfo -TaskName "Job Agent Daily"
Unregister-ScheduledTask -TaskName "Job Agent Daily" -Confirm:$false
```

## 3. Alternative: systemd inside WSL

Only works when `systemd=true` is set in `/etc/wsl.conf` and WSL stays running.
Task Scheduler is more reliable on a laptop that sleeps.

```ini
# ~/.config/systemd/user/job-agent.service
[Unit]
Description=Job Agent daily discovery (never submits applications)

[Service]
Type=oneshot
WorkingDirectory=%h/job-application-agent
ExecStart=%h/job-application-agent/scripts/run_daily.sh
```

```ini
# ~/.config/systemd/user/job-agent.timer
[Unit]
Description=Run Job Agent once per day

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now job-agent.timer
systemctl --user list-timers job-agent.timer
```

## 4. What to check after the first scheduled runs

```bash
tail -60 logs/daily-$(date +%Y-%m).log      # stage counters and warnings
uv run job-agent list --limit 30            # what landed in the tracker
uv run job-agent approved                   # still prints "no applications submitted"
```

Expect `Applications submitted: 0` in every log, forever. If a run reports
`Warnings:`, the lines name the stage (`[discovery]`, `[description]`,
`[matching]`, `[sheets]`) and the source, never a response body or a key.

## 5. Turning it off

Disable the task (`Disable-ScheduledTask -TaskName "Job Agent Daily"`) or
`systemctl --user disable --now job-agent.timer`. Nothing else is required; the
agent keeps no server-side state.
