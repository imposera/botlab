# TB Race Queue Wall

Start from `~/botlab/totebot`:

```sh
python3 scripts/tb_race_queue_wall.py
```

Open http://127.0.0.1:8792. Python standard library only. Keep the HTML file beside the Python script.

The wall reads `state/tb_race_queue.json` every 10 seconds and updates scheduled countdowns every second. It preserves queue order, offers Upcoming / Review / Removed / All views and country / track filters, and shows recommendation reasons when a race is selected. Times use the browser's local timezone. State age uses the queue's `updated_at` timestamp (including decision updates); it is not a Betfair discovery heartbeat. State older than 120 seconds is marked stale.

Keep / Remove / Restore use the existing queue decision function and JSONL audit log. Restore clears the human decision. Only Remove excludes a race. The wall does not call Betfair or place orders. Refresh the underlying queue separately using the existing workflow, or:

```sh
python3 scripts/tb_race_queue.py refresh
```

Options: `--state-dir PATH`, `--host HOST`, `--port PORT`. The default loopback listener is intended for local use; there is no user authentication. Remote access should use an SSH tunnel. Requests that change decisions require the page's session token.

## Run with systemd

The user units match the Betfair emitter setup:

- `tb-race-queue.service` refreshes the next 10 races over a four-hour search window, using the existing queue lock and preserving human decisions.
- `tb-race-queue.timer` starts the refresh 30 seconds after boot, then 30 seconds after each run finishes. Refresh runs from this timer do not overlap.
- `tb-race-queue-wall.service` keeps the wall running on `127.0.0.1:8792` and restarts it after a failure.

The refresh service uses `/opt/betfair` for gateway imports and `/opt/betfair/secrets.env` for credentials, with optional path overrides in `~/.config/totebot/betfair-services.env`, just like the emitter. The wall needs no Betfair credentials. These units run independently of the emitter and observer and do not change emitter target selection.

From `~/botlab/totebot`, validate and install:

```sh
systemd-analyze --user verify scripts/tb-race-queue.service scripts/tb-race-queue.timer scripts/tb-race-queue-wall.service
mkdir -p ~/.config/systemd/user
cp scripts/tb-race-queue.service scripts/tb-race-queue.timer scripts/tb-race-queue-wall.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Stop any manually started queue wall using port 8792 before starting the wall service. Replace any existing queue refresh cron job or timer to avoid duplicate discovery calls. Run the initial refresh and check that it succeeds before enabling the timer:

```sh
systemctl --user start tb-race-queue.service
systemctl --user status tb-race-queue.service
systemctl --user enable --now tb-race-queue.timer tb-race-queue-wall.service
```

A successful oneshot refresh normally returns to inactive after completion. Check its exit status and journal, and inspect the running timer and wall:

```sh
systemctl --user list-timers tb-race-queue.timer
systemctl --user status tb-race-queue-wall.service
journalctl --user -u tb-race-queue.service -n 50
journalctl --user -u tb-race-queue-wall.service -n 50
```

These units follow the user systemd manager's lifetime. Running through logout or before login requires lingering to be enabled for the account.

To stop automatic refreshes and the wall:

```sh
systemctl --user disable --now tb-race-queue.timer tb-race-queue-wall.service
systemctl --user stop tb-race-queue.service
```

## Tests

```sh
python3 -m unittest discover -s scripts -p 'test_tb_race_queue*.py'
```
