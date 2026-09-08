# ToteBot blackbook

Browse the register at `http://localhost:8787/blackbook` on the wall host,
or use that host's LAN/Tailscale address with port 8787. The race wall includes
a Blackbook link. Search names, selection IDs, tags or notes; filter by track,
minimum wins or tags; sort by latest win, win count or name. Each page contains
up to 50 runners with expandable histories. Reload to see refreshed data.

Country is the race location, taken from explicit result/closure metadata or
stage captures, with aliases such as USA/US and UK/GB normalised. Missing values
display as Unknown. The country filter matches any recorded win; the main
country column shows the last win's country. History shows country per race.

Runner / market matched shows the latest paired capture for the last win.
Expanded histories show runner amount, market amount and runner percentage
share at each stage (T−30s means 30 seconds). Both amounts use the same snapshot.
Runner amounts fall back to summed traded levels when reported totals are
missing or zero. Missing values and invalid shares display as unavailable.
Currency and delayed-feed labels come from the source capture.

The register is derived from completed local history. It records observed wins,
not all starts. Missing or unreadable history preserves existing records.
A completed result with an explicit empty winners list removes previous wins
for that market. Manual tags, notes and status survive even when no wins remain.
An unreadable existing register stops the scan without overwriting it.
Scans share a lock across the complete read/merge/write operation.

The local user timer `tb-blackbook.timer` runs a full scan five minutes after
each run finishes, starting one minute after boot when the user manager runs.
It reads local files only and does not contact Betfair. It depends on history
being collected or synced separately.

Install or update the units:

```bash
install -m 644 tb-blackbook.service tb-blackbook.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tb-blackbook.timer
systemctl --user start tb-blackbook.service
```

Inspect scheduling and failures:

```bash
systemctl --user list-timers tb-blackbook.timer
journalctl --user -u tb-blackbook.service -n 20 --no-pager
```

Preview a rebuild without writing: `python3 tb_blackbook.py scan --dry-run`.
Refresh one corrected market: `python3 tb_blackbook.py scan --market-id 1.123`.
