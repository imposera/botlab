# TB Race Queue Wall

## Learned clash preference — 9 September 2026

Automatic selection now uses the queue's explicit decisions and a conservative learned preference, shared by discovery and the emitter through `tb_queue_policy.py`.

- Evidence is the latest explicit decision for each distinct market in the last 30 days. Restore cancels that market's training label. At least three removals on two distinct scheduled race dates and an 80% removal rate are required, grouped by country, track and broad race class. Repeated clicks do not count as additional races.
- Among the next ten future races, a qualifying race is skipped for automatic arming only if another eligible, non-disfavoured race (or an explicit Keep) starts within five minutes. Two disfavoured races cannot eliminate each other. No alternative means no learned skip. An explicit Keep overrides learning for that race.
- Races stay visible and retain their human decision. `arming_eligible` and `clash_skip` describe the temporary automatic selection result; the wall displays “Clash skip” with the alternative and evidence in race details. Queue order is unchanged. Next eligible uses arming eligibility.
- The emitter recomputes from its fresh market discovery and authoritative decisions/audit, rather than trusting cached queue flags. It respects explicit Remove for new automatic targets and automatic rearm challengers. Learned skips also constrain hold candidates. Scoring-excluded alternatives cannot cause a learned skip. Existing target locks and explicit manual arm requests retain their existing precedence.
- An alternative can be just outside the current T-15 arming window: the emitter waits for it instead of falling back to the skipped race. Existing active targets are not cancelled by learning. The wall is a scheduled-clash preview; emitter scoring and current observation state still determine actual arming.

At implementation time, Mountaineer claiming races qualify with eight distinct removals across two race dates. This is attention selection, not a prediction about race outcomes. Thresholds are currently fixed in the shared policy module. Audit read/parse failures stop new automatic selection instead of applying partial learning.

Validation: ten policy/integration tests, thirteen queue tests, four emitter tests and three HTTP wall tests passed. Tests cover lone races, timing bounds, explicit Keep/Remove, deduplicated evidence, single-meeting evidence, Restore, expiry, race-class/country scope, unavailable alternatives, the next-ten limit and immediate queue updates.

Deployed on basecamp at 11:13 Melbourne: queue version `1.2.0`, emitter version `1.3.0`. The 27 policy/queue/emitter tests also passed in basecamp's Python environment. Live discovery and emitter runs succeeded; both timers and the wall were active, and the tunneled page/API exposed the new policy. No learned clash skips were present in that snapshot. The existing armed market `1.262116318` remained active under the existing-target rule. Previous code is backed up at `/home/basecamp/botlab/totebot/queue-recovery-backups/clash-code-20260909T011320Z`.

## Deployed correction — 9 September 2026, 10:56 Melbourne

Basecamp is the queue authority: its refresh timer and queue wall both use the same local state and lock. On core7070, `tb-race-queue-proxy.service` replaces the former Python wall in tmux. It forwards port 8792 to basecamp's loopback wall over the host-configured `ssh basecamp` alias (identity `~/.ssh/core7070`). The browser URL remains `http://core7070:8792`. Reload any page opened before the change to obtain the authority's session token.

The proxy serves the authority's page, reads and decisions directly. It never writes replicated state. An unavailable SSH connection makes the queue wall unavailable; there is no fallback local writer. Systemd restarts the tunnel after failures, and SSH keepalives detect a lost connection. The all-interface listener preserves the previous local wall's access scope; the upstream wall remains loopback-only.

Mountaineer R2/R3 removals were recovered under the authority's queue lock from the latest existing audit events, preserving their original reasons and event timestamps without adding new human actions. Backups and a recovery manifest are on basecamp at `/home/basecamp/botlab/totebot/queue-recovery-backups/20260909T005514Z`. All ten conflict copies were retained.

Verification: the tunnel unit passed systemd validation and is enabled/active with zero restarts; local and upstream page SHA-256 hashes match; a decision without a session token returns HTTP 403; an explicit discovery refresh succeeded and retained both recovered decisions. The local replica received all 25 decisions, with the conflict count still ten at verification.

The shared `state` folder remains bidirectional because it contains other application state. Queue replication is now outward in normal operation because the deployed local wall no longer writes it; this is not a Syncthing per-file write restriction. Treat queue replicas as read-only: do not run local queue decision/refresh commands or start additional standalone walls against replicated state. Any additional browser should use the authoritative wall through this tunnel. Enforcing replica permissions across other devices would require a separate queue share or broader sync-layout change.

To install this reader-host tunnel (requires a working `ssh basecamp` alias), stop any existing local queue wall on port 8792 first:

```sh
systemd-analyze --user verify scripts/tb-race-queue-proxy.service
install -D -m 644 scripts/tb-race-queue-proxy.service ~/.config/systemd/user/tb-race-queue-proxy.service
systemctl --user daemon-reload
systemctl --user enable --now tb-race-queue-proxy.service
```

The standalone wall and discovery installation instructions below apply to the authoritative host or isolated development state, not reader replicas.

## Operational status — 9 September 2026, 10:39 Melbourne

The local replicated queue is updating: its timestamp advanced during this review. It contains 10 races, with six eligible and four explicitly removed Mountaineer Park races. The next eligible race is Cambridge R1 1300m Maiden at 11:09 Melbourne time. Recommendations comprise three KEEP, three REVIEW and four PRUNE? entries; these recommendations do not change eligibility.

Discovery runs separately on `basecamp-vps`, as documented in `tb-wall_README.md`. No queue systemd units were found on this local host. Remote service health could not be verified because SSH to `basecamp@basecamp-vps` failed with `Permission denied (publickey)`.

Ten queue sync-conflict copies are present locally, including one from this morning. The investigation below found lost historical decisions; the active queue retains the four recent removals. No decisions or conflict files were changed during this review.

### Sync-conflict investigation — 9 September 2026

Local Syncthing configuration shares `state` as `sendreceive` (folder `qvyk6-iwwyd`) with core7070, basecamp, asus and Razr. The conflict filename device prefixes map to core7070 (`HMC7J34`) and basecamp (`V2XW7D2`). The filesystem watcher delay is 10 seconds.

Both `refresh_queue` and `update_decision` in `tb_race_queue.py` replace the entire queue JSON. The file lock coordinates local application processes only; it does not coordinate remote writers or Syncthing replacements. This is consistent with refreshes and human decisions on different replicas producing conflicting snapshots. Remote process placement remains unverified because SSH authentication failed.

Comparison of all ten conflict copies against the active queue and the latest audit event per market found:

| Market | Race | Finding |
| --- | --- | --- |
| `1.262116313` | Mountaineer R2, 9 September 09:25 Melbourne | REMOVE in audit and 07:54/07:55 conflict copies; absent from active decisions |
| `1.262116314` | Mountaineer R3, 9 September 09:50 Melbourne | REMOVE in audit and 07:55 conflict copy; absent from active decisions |
| `1.262116316`, `1.262116317` | Mountaineer R5/R6 | Early removals in conflict copy; active queue contains later removals from 10:35 |
| `1.262063380`, `1.262063383` | Previous day's Mountaineer R4/R7 | Conflict copies contain earlier timestamps for removals retained in active queue |

The missing R2/R3 removals are also absent from the 10:35 conflict snapshot. Both races had already passed at investigation time. The refresh implementation preserves the entire decisions dictionary, so normal expiry does not explain these missing entries. The audit log has no later restore for either market. There were no additional action mismatches against the available audit log, and no local conflict copies of that log.

Recommended correction: make one host authoritative for both discovery and decisions, with every wall submitting decisions to that host. Replicate the queue outward for readers only. Avoid switching the entire shared `state` folder to receive-only without checking its other writers. Historical recovery should replay the latest audit decisions on the authoritative host after writer ownership is resolved. Retain the conflict copies until recovery is verified; deleting them alone will not prevent recurrence.

On the authoritative host or with isolated development state, start from `~/botlab/totebot`:

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

The refresh service uses `/opt/betfair` for gateway imports and `/opt/betfair/secrets.env` for credentials, with optional path overrides in `~/.config/totebot/betfair-services.env`, just like the emitter. The wall needs no Betfair credentials. These units run independently; the emitter now reads the authoritative queue decisions and audit for automatic selection.

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
