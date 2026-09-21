# Australian coverage first release

The basecamp-only `tb-au-observer.service` observes every Australian thoroughbred WIN market retained by `tb_today_card.py`. Track order, emitter priority, Keep/Remove decisions and active target locks do not filter these read-only background observations. The existing emitter and single-target observer remain independent; this service never writes their state or history.

Every market receives five durable jobs: T15, T10, T5, T2 minutes and T30 seconds before scheduled start. A ten-second service loop collects due jobs, using two workers and batches of at most five markets. A shared request-start gate permits at most one request per second across those workers. EX_BEST_OFFERS costs five points per market (25 points per batch, below Betfair's 200-point limit). Existing services retain their own request behavior; this is not an account-wide gateway replacement.

Successful OPEN/preplay responses are saved under `observations/au/YYYY-MM-DD/MARKET_ID/T15.json` (and other slots). Artifacts include scheduled due time, actual receive time, lateness, full raw market book, market metadata and the delayed-feed flag. They are separate from the current card's historical snapshot format. Capture jobs are in `state/tb_au_observations.json`, written atomically by one locked writer. Captures commit before job completion; restart recovers an already-written artifact without calling Betfair again. A separate lock prevents a second worker service on basecamp.

Missing responses, suspension or network failure are retried within a 60-second grace window. Captures are never backfilled after that window, after scheduled start, or from a response reporting in-play/closed. Missed jobs persist with reasons. A delayed start does not extend scheduled snapshot windows in this release. Captures before installation or discovery cannot be reconstructed. Metadata absent from a later registry response does not delete existing jobs. Jobs from previous dates remain retained in state in this first release; monitor state growth before long-term rollout.

`GET /api/au-coverage` is available on TB Wall (8787) and the authoritative queue wall (8792). It reports current-day registry races, per-slot captures/gaps, per-race scheduled/observing/complete/incomplete/unavailable counts, observer heartbeat and discovery freshness. Missing observer state returns HTTP 503. Existing stale state is returned with a stale flag. Coverage means captures of published registry races, not a guarantee that Betfair has published every real-world race. Delayed feeds are explicitly marked. The collapsed Australian coverage panel below the daily card on TB Wall updates during normal polling.

Deployment: install the two new Python modules and service on basecamp alongside existing queue/card modules, then `systemctl --user daemon-reload` and `systemctl --user enable --now tb-au-observer.service`. Install `tb_au_coverage.py` and the updated `tb_wall.py` on core7070 and restart TB Wall. Install the updated `tb_race_queue_wall.py` and restart the authoritative queue wall. The existing state replication carries coverage data to core7070; the new service must run only on basecamp.

Validation: `python3 -m unittest test_tb_au_observer` from scripts. Includes simultaneous tracks and removed races, idempotent recovery, retry and late gaps, in-play/closed rejection, retained omitted races and day rollover, corrupt state preservation, complete five-slot capture, stale/unavailable coverage, escaping and HTTP routes.

First release enabled on basecamp on 11 September 2026 at 11:19 AEST. All 41 Australian registry races received five jobs each (205 total), before the earliest T15 window at 12:20 AEST. The 96-test combined observer/card/queue/wall/emitter suite passed. Existing queue wall code was backed up at `queue-recovery-backups/au-observer-20260911T011900Z`.

## Australian Capture Wall

Open `/au-captures` on either TB Wall (8787) or the authoritative queue wall (8792). Choose a retained date and race to compare runner last-traded prices and best back/lay across T15, T10, T5, T2 and T30. The timing table shows expected and received timestamps, lateness, feed flags, gaps and raw JSON links. `/api/au-capture?date=YYYY-MM-DD&market=MARKET_ID&slot=T15` returns one validated saved artifact. Invalid selections return 400; unavailable captures return 404. Reads never modify jobs, queue decisions or captures. Reload explicitly for newly collected snapshots. The local wall uses replicated artifacts and marks files unavailable when job metadata arrives before the artifact.

Links are provided beside daily-card races, in coverage rows, and in the authoritative card's selected-race details. Previous dates remain selectable from the persistent observation registry. Capture-wall release validated with 35 viewer, coverage, card and wall tests, including escaping, missing artifacts and path validation.

## Runner details and liquidity upgrade

The daily catalogue now requests and preserves RUNNER_METADATA keyed by selection ID. The Capture Wall displays the latest retained barrier, jockey, trainer and carried weight (with supplied units). Availability varies; absent values remain —. Older race metadata and captures are not backfilled.

New snapshots request EX_BEST_OFFERS plus EX_TRADED, recording the requested projections in `price_data`. Five-market batches now use 100 of Betfair's 200 weighted request points. Worker count and the shared one-request-per-second gate remain unchanged. The viewer shows money available at the best back/lay prices, spread, cumulative traded volume summed across returned traded price levels, and changes between adjacent available snapshots. A negative volume change is marked reset/reduction rather than positive growth. Missing traded projections in old captures are unknown, not zero. The original delayed-feed flags remain visible. Validation: 36 capture/card/coverage/wall tests passed.

## Snapshot price review

The Capture Wall adds Class, Early % (T15 to T5) and Late % (T5 to T30). Classes describe saved last-traded prices: Stable, Steady firm/drift, Late firm/drift, Firm then rebound, Drift then recovery, and Mixed / volatile. Click a class to expand traded-volume increases, endpoint spreads, missing stages and delayed-feed notes. Negative price changes mean firming.

The default stable range is below 3% of the first available price. Significant adjacent changes use the same threshold; two or more direction reversals are mixed. A late move must reach the threshold and be at least twice the absolute early percentage change. Optional `state/au_price_review_config.json` accepts `{"stable_percent":3}` (greater than zero and at most 20); absent or invalid values use 3. This is descriptive classification, not a predictive rating or an arming rule.

Missing endpoints leave the corresponding percentage blank, missing stages mark the class partial, and no adjacent price pair gives insufficient data. A detected scratching makes the sequence not comparable and suppresses early/late percentages. Older captures without traded projections show unknown volume. Validation: `PYTHONPATH=scripts python3 -m unittest test_tb_au_price_review test_tb_au_capture_wall`.

## Australian race results

`tb-au-results.service` runs only on basecamp and writes `state/tb_au_results.json`, replicated to the local Capture Wall. It reads the retained AU registry independently of the snapshot collector. Each request asks for one market with no price projection, avoiding Betfair's omission of closed markets in mixed open/closed requests. Requests are spaced by at least two seconds, at most 20 per cycle, with a 30-second pause between cycles. Due jobs are ordered by oldest check to avoid starving backlog races.

Checks start two minutes after scheduled start, retry every five minutes for up to seven days, and save full closed market books. Settled results are rechecked hourly until 24 hours after scheduled start to catch early settlement corrections; later corrections require a future refresh upgrade. Older unresolved races are marked unavailable. Saved results remain retained. No results are inferred from prices or missing responses; failures preserve saved results and show a latest-check-failed note.

The wall shows settlement status on meeting race selectors, winner names above the runner table, and a Result column joined by selection ID. Multiple WINNER runners are all displayed. LOSER is labelled Non-winner, not a finishing position; REMOVED is labelled Scratched. A closed market without a reported winner is explicitly labelled that way, not assumed abandoned. This win-market release does not provide second/third places, margins or official finishing order. Refresh the page for newly saved results. Validation: `PYTHONPATH=scripts python3 -m unittest test_tb_au_results test_tb_au_capture_wall`.

## T30 Capture Wall refresh

A selected race without a readable T30 artifact checks its rendered page every ten seconds, starting 45 seconds before scheduled start. Once that wall can render the T30 artifact, the browser reloads the selected race once. Already-captured T30 pages do not poll. Hidden tabs defer checks; network errors retry. Checks stop ten minutes after scheduled start, bounding retries for missed snapshots or synchronization problems. No collector, emitter or queue behavior changes. The reload resets expanded details. Reload an already-open old-version page once to enable this behavior.
