# Today's Australian card

The daily card collects Australian thoroughbred WIN markets for the Melbourne calendar day. It is a browsing and decision view alongside the rolling worldwide queue. It does not change the emitter's country scope, target locks or arming windows.

## Operation

Basecamp alone runs `tb-today-card.timer` / `tb-today-card.service`. The timer polls market status every 60 seconds after the preceding run completes. Meeting/catalogue discovery runs on the first poll, at day rollover and approximately every five minutes. Discovery uses Betfair's `marketCountries: ["AU"]`, `raceTypes: ["Flat", "Steeple", "Hurdle"]`, horse-racing event type and WIN market filters. Date boundaries use `Australia/Melbourne`, including 23/25-hour daylight-saving days.

- `state/tb_today_card.json` holds today's discovered markets, status, runner names and confirmed winners.
- `state/today_cards/YYYY-MM-DD.json` retains each day's card. Closed races are retained even when catalogue discovery no longer returns them. Markets already closed before initial discovery cannot be reconstructed from the catalogue.
- `discovery_updated_at` records successful catalogue discovery. `status_checked_at` records a status polling attempt; each race has its own `status_updated_at` which advances only on a returned market book. Decision updates do not change any of these timestamps.
- A missing market-book response is retried individually, since Betfair omits closed markets from mixed open/closed requests. Missing responses or elapsed scheduled times never imply completion. CLOSED with a WINNER is displayed as Completed; CLOSED without a winner stays Closed.
- Discovery older than ten minutes or nonterminal race status older than three minutes is stale. Feed errors and missing market IDs are exposed. Delayed data is also identified on individual race details. Old dates are not displayed as today's card.

The card's state files replicate through the existing state share. Reader hosts must not run the collector or write those files. Do not switch the entire state share's sync direction for this feature.

## TB Wall and API

TB Wall `1.7.0` includes a collapsed **Today's Australian card** directly below the existing Next eligible disclosure, under the runners. Expanding it shows meeting and view filters, grouped race links, scheduled Melbourne times and race/selection status. It participates in the existing 15-second polling, retaining disclosures and filter selections. Historical race pages omit it.

`GET /api/today-card` on port 8787 is read-only. The same endpoint is available on the authoritative wall through port 8792. Optional query parameters:

- `meeting=<meeting_id>` filters to one meeting.
- `view=all|upcoming|clashes|review|completed` filters races. Review includes explicit removals and currently reported clash skips. Completed also includes closed markets without a confirmed winner.

Responses use `schema: tb_today_card_summary/v1` and contain the date, timezone, freshness/timestamps/errors, meeting catalogue, total meeting/race counts and filtered race rows. Race state is separate from `selection_status`, so an already armed race can still display a Remove decision. `decision_allowed` is true only for a future race with sufficiently fresh discovery and OPEN/SUSPENDED, non-in-play status. HTTP 503 means today's card is missing/invalid; HTTP 200 with `ok: false` means cached information. Arming remains the emitter's responsibility. Future races outside the rolling queue show their explicit decision; clash skips are displayed only when reported by a fresh rolling queue.

The TB Wall meeting filter uses `card_meeting` and `card_view` query parameters. Other consumers can import `tb_today_card.summary(state_dir, now=..., meeting=..., view=...)` without network calls or writes.

## Shared decisions

Race links open `http://core7070:8792/today?market=<market_id>`. The new daily-card page reads the authority directly and offers Keep/Remove/Restore, meeting filters, race details and winners. The rolling queue page links to it too.

Authenticated page-session requests POST to `/api/card-decision` with the existing `X-Queue-Token`, JSON `market_id`, `action` and optional `reason`. The server checks that the market is on today's current card and has not started. It calls the existing locked queue decision function, storing decisions in `tb_race_queue.json` and audit events in `tb_race_queue_decisions.jsonl`. A card snapshot supplies the audit context for races outside the next ten; no duplicate queue entry is inserted. The emitter already reads those explicit decisions for future automatic target selection. Restore removes the shared decision. No decision write is accepted by TB Wall on port 8787.

## Installation

On basecamp, deploy `tb_today_card.py`, `tb_today_card.html`, `tb_queue_summary.py`, and the updated `tb_race_queue.py`, `tb_race_queue_wall.py`, `tb_race_queue_wall.html`. Keep the existing gateway and queue policy dependencies available.

```sh
systemd-analyze --user verify scripts/tb-today-card.service scripts/tb-today-card.timer
install -D -m 644 scripts/tb-today-card.service ~/.config/systemd/user/tb-today-card.service
install -D -m 644 scripts/tb-today-card.timer ~/.config/systemd/user/tb-today-card.timer
systemctl --user daemon-reload
systemctl --user start tb-today-card.service
systemctl --user enable --now tb-today-card.timer
systemctl --user restart tb-race-queue-wall.service
```

On core7070, deploy `tb_today_card.py` alongside `tb_queue_summary.py`, the updated `tb_wall.py` and `tb_wall_poll.js`, then restart `tb-wall.service`. Do not install the collector timer there.

## Validation

```sh
cd scripts
python3 -m unittest test_tb_today_card test_tb_race_queue_wall test_tb_race_queue test_tb_queue_policy test_tb_queue_summary test_tb_wall
```

The suite covers discovery filters and exclusive day boundaries, daylight saving, closed-race retention, omitted-book recovery, failure freshness, rollover archives, shared decisions outside the rolling queue, authentication, filtering and read-only rendering. The initial isolated basecamp discovery on 11 September returned 32 races across four meetings.

First release deployed 11 September 2026 at 08:14 AEST. The collector timer, queue timer, queue wall and existing emitter timer were active after deployment. The first production collection succeeded and replicated 32 races: Geelong 9, Tuncurry 8, Goulburn 7 and Sunshine Coast 8. The 51-test Python suite passed. Live HTTP checks verified the API, Geelong filtering and panel placement; Firefox checks verified all 32 rendered races, race deep links, the nine-race meeting filter, and preservation of the card's open state and meeting selection during background refresh. Browser checks did not submit live race decisions. Previous authoritative code is backed up at `/home/basecamp/botlab/totebot/queue-recovery-backups/daily-card-20260910T221428Z`.

Betfair references: [market filters and race types](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687465/Betting+Type+Definitions), [market books and closed-market request behavior](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687510/listMarketBook), [request limits](https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687478).

## Emitter forecast and timing clashes — 11 September 2026

TB Wall 1.8.0 and the authoritative daily-card page display a read-only emitter rehearsal. Each card race has a projected sequence number or reason it is not selected, its T−18 to T−12 arming window (using current target parameters), overlapping races and delay sensitivity. Red marks a projected timing block; amber marks overlapping observation windows or a selection that changes under the three-minute delay scenario. The Timing clashes filter shows affected races across meetings. The expandable sequence lists projected arming times in Melbourne time. These highlights never write Remove decisions or change targets.

The collector builds `emitter_forecast` once per card refresh, using a private import of the deployed emitter's existing selection, scoring, hold and rearm functions. It reads the effective priority configuration, historical liquidity, explicit decisions and learned preferences. A currently held target is included and actual hard-lock evidence is respected. Future locks are approximated at their configured snapshot times. The simulator runs two scenarios: release at scheduled start, and release three minutes later. It uses 30-second model ticks, matching the reviewed timer interval but omitting process runtime and actual timer phase. Real start confirmation, feed delays, manual requests, scratchings and snapshot availability can change the result. The three-minute case is sensitivity analysis, not the emitter's maximum observation hold.

Scope is today's remaining Australian card plus future races available in the fresh worldwide rolling queue. Other overseas races not yet discovered in that queue are not forecast. The global emitter remains worldwide. A forecast is a conditional schedule, not a promise to arm every projected race. Old forecast rows are marked cached when discovery/status is stale, the forecast is older than three minutes, the shared decisions change, or the current armed target changes. The next collector run recomputes it; neither reader host nor the decision page runs a second writer.

The `/api/today-card` response now includes `emitter_forecast` metadata, warnings, effective mode, assumptions, sequence and counts. Each race contains its own `emitter_forecast` result and `forecast_fresh`. Timing-clash geometry is calculated across the full remaining AU card before meeting/view filtering. `view=clashes` selects races with overlapping scheduled observation windows; it does not imply that every highlighted race is skipped.

At review, basecamp's `config/race_priority.json` had a missing comma after the Wangaratta score, before Tuncurry. The live emitter's loader therefore used earliest-race defaults. The forecast reproduces that fallback and displays the warning; this release does not edit the configuration. The initial rehearsal projected 24 AU selections and 8 timing blocks, with 24 card races involved in overlapping windows. No selected race was lost in the three-minute sensitivity case at that snapshot.

Deploy `tb_card_forecast.py` together with the updated `tb_today_card.py` and `tb_today_card.html` on basecamp, then restart the daily-card collector and authoritative queue wall. Deploy the two Python modules and updated `tb_wall.py` on core7070, then restart TB Wall. The private emitter module is loaded only in the collector; read-only summaries do not import the gateway or call Betfair.

Validation: the 64-test forecast/card/queue/wall/emitter suite passed. Nine new tests exercise ten-minute timing blocks, delay sensitivity, explicit Remove and Keep, learned skips, priority scoring, protected active targets and rearm, known overseas competitors, window boundaries, and invalidation after changed decisions.

Forecast display deployed at 08:49 AEST on 11 September. Both authoritative and replicated APIs reported 24 projected selections, 8 timing blocks (Geelong R2–R9), and 24 races with overlapping windows at verification. Live Firefox checks confirmed numbered forecasts, eight red rows, the 24-race Timing clashes filter, and preserved disclosure/filter state on TB Wall. Previous card code is backed up on basecamp at `/home/basecamp/botlab/totebot/queue-recovery-backups/card-forecast-20260910T224934Z`. Emitter code and priority configuration were not changed by this forecast release.

## Australian meeting priority

`config/australian_card_priority.json` on basecamp controls card grouping and optional emitter clash preferences. Copy `australian_card_priority.example.json` to that path. The configured order is Geelong VIC, Tuncurry NSW, Goulburn NSW; unlisted meetings follow in first-jump order. This is a track list, not a statewide ranking, and persists until edited.

With `apply_to_emitter: true`, automatic Australian candidates from lower-ranked tracks are skipped when their scheduled observation windows overlap a higher-ranked candidate (less than 18 minutes between jumps with the default T−15 ±3 settings). Higher ranks are resolved first, so a skipped race cannot suppress another. Non-Australian markets retain existing selection rules. Explicit Keep exempts a race from this skip; Remove, configured exclusions, and an already-missed arming window prevent a race from being the preferred alternative. Existing manual selection and armed-target lock rules retain precedence. The rule does not create Remove decisions.

The rolling Next eligible list applies the same default-window preference. The daily card snapshots the configuration for read-only TB Wall display, and the emitter forecast shows track-priority skips separately from timing blocks. The forecast sequence remains chronological even though the card groups races by track rank. Missing configuration disables the rule; invalid configuration disables emitter preference and produces a forecast warning while the card retains its last valid order.

Validation: 70 regression tests passed, including ordering, labels, country boundaries, future higher-ranked races, Keep/Remove, excluded alternatives, non-overlapping races, skip-chain avoidance, and emitter/queue/forecast agreement.
