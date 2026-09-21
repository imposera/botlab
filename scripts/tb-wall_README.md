# ToteBot wall service

`tb-wall.service` runs the main race wall on core7070, listening on port 8787 with the same all-interface binding as the previous tmux launch. It reads replicated state and history, and needs no Betfair credentials or timer. The queue discovery timer runs separately on basecamp-vps.

## Australian daily card — 11 September 2026

Version `1.7.0` adds a collapsed Australian daily card below Next eligible, under the runner list. It provides meeting/view filters and race links to the authoritative Keep/Remove/Restore page. The read-only `/api/today-card` endpoint exposes the same summary. Basecamp runs the new daily-card collector; the worldwide arming scope stays unchanged. Deploy `tb_today_card.py` and the updated `tb_wall_poll.js` with the wall. See [daily-card operation and API](tb_today_card_README.md).

## Queue summary — deployed 9 September 2026, 11:30 AEST

TB Wall version `1.6.0` adds a live-only Next to go panel directly below the runner list, before the shape legend, observations and volume. It is collapsed by default; expand Next to go to see the reported armed target separately, then up to five upcoming eligible races in authoritative queue order. Background refresh preserves the open/closed state. Skipped and removed races are available in a disclosure with their reasons. Manage queue opens `http://core7070:8792`, the existing tunnel to basecamp's authoritative wall. The panel never changes decisions or trains preferences.

The shared function is `tb_queue_summary.summary(state_dir, now=None, limit=5)` (the optional arguments are keyword-only). It reads `tb_race_queue.json` and `betfair_t15_target.json`, consumes saved `arming_eligible`/`clash_skip` policy results, recalculates countdowns and omits elapsed races and the reported armed market from the upcoming lists. `tb_queue_summary.panel(summary)` renders the same structure. Include `tb_queue_summary.py` when deploying the wall.

Other consumers can GET `http://core7070:8787/api/queue-summary`. The response schema is `tb_queue_summary/v1`:

- `generated_at`, `queue_updated_at`, `age_seconds`, `freshness`, `ok`, `message` describe saved-state freshness.
- `armed` is the separately reported armed target, with its own timestamp, age and freshness; it can be null.
- `next_eligible` is the first eligible upcoming race only when queue state is fresh; it is not an arming commitment.
- `eligible` contains up to five rows; `excluded` contains upcoming skipped/removed/unknown-eligibility rows. Each has identity, scheduled time, countdown, status, human action and reason; clash rows identify the alternative market.
- `counts` covers all upcoming non-armed rows, before the display limit. `limit` is five for the HTTP endpoint.

Queue timestamps older than 120 seconds are stale. Unknown or future-skewed timestamps are marked unknown. Stale/unknown responses retain cached rows but set `ok: false` and `next_eligible: null`; HTTP status remains 200. Missing or invalid queue data returns structured JSON with HTTP 503. Responses use `Cache-Control: no-store`. Queue age is based on its saved `updated_at`, which can also advance on decisions; it is not a separate discovery heartbeat. Target and queue files replicate independently, so the summary is not an atomic cross-file snapshot.

The panel participates in the existing 15-second page polling through `data-poll="queue"`; open disclosures remain open. Historical race pages omit the live queue. Five summary/integration tests and eleven wall tests passed, including stale/missing data, escaping, ordering, separate armed/excluded rows, read-only behavior and the endpoint. After restart, the live page, endpoint and `/health` were checked successfully.

## Current status — reviewed 9 September 2026, 09:01 AEST

- Service enabled and running since 8 September at 13:43 AEST; `/health` responds successfully and reports version `1.5.1`.
- At the check, the selected market was Louisiana Downs R5 (`1.262117779`), target status `armed`. The target file was 18 seconds old and the active book was 286 seconds old. Market data was marked delayed. These are observations at the review time, not a continuing freshness guarantee.
- Working-tree code includes the shared `tb_runner_shape.py` integration: four fixed directional intervals, with `?` for missing endpoints, and an updated legend. These edits were loaded by the service restart at 09:02:30 AEST on 9 September; the served page was checked for the updated missing-interval legend. Version `1.5.1` alone does not distinguish the running code from the edited files. Include the shared module when deploying, and restart the service to load Python changes.
- Validation: `python3 -m unittest test_tb_wall test_tb_runner_shape test_tb_blackbook_wall test_tb_review` from `scripts/` passed all 36 tests.
- Review limitation: `/health` returns `ok: true` even when input files are absent or stale; its age fields must be inspected separately. The wall's green file-age chips indicate file presence, not freshness. The health payload reports the target/active-book market, whereas the live page can use a fresh observation book. A successful health response therefore confirms HTTP availability, not current race-data quality.

Following the review, the service was restarted at the user’s request at 09:02:30 AEST. Both `/health` and the main page responded successfully after startup. Existing working-tree changes were preserved.

Install from `~/botlab/totebot`:

```sh
install -D -m 644 scripts/tb-wall.service ~/.config/systemd/user/tb-wall.service
systemctl --user daemon-reload
```

Stop the old tmux wall process before enabling the service:

```sh
systemctl --user enable --now tb-wall.service
systemctl --user status tb-wall.service
journalctl --user -u tb-wall.service -n 50
```

Open http://core7070:8787. The separate race queue wall uses port 8792.

Live views poll the current URL every 15 seconds and patch only changed components. Applied filters remain in the request URL; unsaved filter edits, open disclosures, and scroll positions are preserved. Historical views do not poll. Failed requests leave the last data visible with a retry status. Background tabs pause polling and check immediately when visible again. Keep `tb_wall_poll.js` beside `tb_wall.py` when deploying. Polling reuses the existing HTML renderer; it still fetches full HTML but does not reload the browser page.

TB Wall `1.8.0` adds daily-card emitter forecasts, projected sequence numbers, red/amber timing highlights and a Timing clashes filter. Deploy `tb_card_forecast.py` with `tb_today_card.py`. Forecasts are generated on basecamp and replicated; read-only displays do not simulate or change arming. See the daily-card README for assumptions and the priority-config warning.

## Australian coverage (v1.9.0)

The collapsed Australian coverage panel below the daily card shows independent scheduled observations for all Australian registry races. Track priority and queue Remove decisions affect preferred-target selection, but do not suppress these background captures. `/api/au-coverage` exposes the same counts, per-race slots, gaps, delayed-feed flags and freshness. The basecamp-only service and recovery behavior are described in [tb_au_observer_README.md](tb_au_observer_README.md).
