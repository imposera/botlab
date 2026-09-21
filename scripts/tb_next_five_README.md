# Next five Australian races

`/next-five` displays the next five future scheduled Australian races in chronological order, independently of track preferences and shared Keep/Remove decisions. Queue decisions remain visible. Started, in-play and closed races are excluded. Races without usable snapshots remain in the table with an explicit missing-data message. `/api/next-five` exposes the same read-only summary. Both routes are available on TB Wall (8787) and the authoritative queue wall (8792).

## File-level design and data flow

- `tb_au_snapshot_analysis.py`: shared validated artifact reader, existing complete-book market probabilities, numeric parsing, and time-bounded runner shortlist. Existing `tb_au_capture_wall` imports continue to work.
- `tb_next_five.py`: read-only Australian card selection, freshness, JSON endpoint and HTML presentation. Refreshes every ten seconds while visible; refresh can be disabled. Failed refreshes visibly mark cached content. Disclosure state follows market identity as races roll over.
- `tb_au_capture_wall.py`: removes Top winner profiles and highlights the same top three qualifying runners with gold names. Other runner columns and historical review remain available.
- `tb_wall.py`, `tb_race_queue_wall.py`: route the new page/API.
- `tb_today_card.html`, `tb_race_queue_wall.html`: navigation links. TB Wall and Capture Wall also link to Next five.
- `test_tb_next_five.py`: focused feature and HTTP tests.

Basecamp is the sole writer: `tb_today_card.py` discovers/status-checks the Australian WIN thoroughbred card; `tb_au_observer.py` saves T15, T10, T5, T2 and T30 captures under `observations/au/DATE/MARKET/`; manual recent-form imports supply race-specific `state/tb_ra_form.json`. Core7070 reads replicated state and captures. The deployed capture-wall and classifier SHA256 hashes matched this checkout before implementation. Existing collector writes are atomic and locked; this feature writes no derived state and needs no writer, job, lock or migration.

## Qualification and meanings

This release uses the existing descriptive classifier, not winner-profile training. Qualifying classes are Steady firm/drift, Late firm/drift, Drift then recovery and Firm then rebound (firm then drift). Stable, mixed, insufficient and scratching-broken histories do not qualify. At least one adjacent scheduled pair must be present. The shared stable threshold defaults to 3%, retaining the existing optional `state/au_price_review_config.json` setting.

Candidates must have a valid active-runner price at the latest accepted **market** snapshot. Rank first by number of captured prices, then by `(maximum price − minimum price) / first price`, descending. Selection ID resolves ties. Display at most three; do not pad with non-qualifiers. Partial shapes are explicitly provisional and can change with subsequent snapshots. This ranking identifies observations worth inspecting; it does not rank expected winners.

- **Base:** frozen race-specific recent-form baseline, shown only with dated prior starts and an import recorded before assessment and scheduled start. Later imports and unknown provenance remain missing. Current cross-race horse database values are not substituted.
- **Chance / Fair odds:** existing earliest complete preplay market-book normalization, identical to the capture wall. One common snapshot supplies all runners. Fair odds = 100 / Chance %. These are market-implied references, not a prediction derived from Base.
- **Move:** first-to-latest available price percentage change; negative means firming.
- **Class:** shared snapshot classification.
- **Latest:** last-traded price and named snapshot, not an executable quote. Capture time does not establish the age of the last trade.
- **Coverage / feed:** partial/full coverage and any delayed-feed flag in the comparison. Per-race details expose exact received/due timestamps, lateness, missing/invalid stages and unknown feed flags.

Captures must have valid identities, OPEN/non-in-play books, unique runner IDs and increasing actual receive times, and must have arrived no later than assessment and strictly before scheduled start. Invalid/missing stages stay missing; they are not interpolated. A newly observed scratching breaks comparison for the entire market and suppresses qualification. Stale card and observer state remain visibly stale. Missing or invalid current-day card produces API HTTP 503; the HTML remains available with a message and refresh controls. State/artifact replication lag is displayed as missing data. At restart the same inputs and assessment time give the same output without modifying files.

Historical winner results/models are not consulted by this shortlist. The historical capture view applies the same pre-start capture/form cutoff. Existing unrelated historical columns retain their previous behaviour. This is a descriptive release: no learned thresholds, additional sampling, betting, emitter changes, automatic Keep/Remove or observation-history changes. It intentionally does not carry overdue scheduled races into the next-five list.

## Validation

```sh
PYTHONPATH=scripts python3 -m unittest test_tb_next_five test_tb_au_capture_wall test_tb_market_chances test_tb_au_price_review test_tb_au_observer test_tb_today_card test_tb_wall test_tb_race_queue_wall test_tb_runner_shape test_tb_historical_intelligence test_tb_ra_form test_tb_race_queue test_tb_queue_policy test_tb_queue_summary test_tb_card_forecast test_betfair_t15_emit
```

120 tests pass (20 new feature tests). Coverage includes complete data, missing fields/stages, stable/mixed shapes, chronological ordering, preserved Remove decisions, scratchings, delayed feeds and exact capture times, stale/corrupt state, wrong dates, duplicate/non-monotonic captures, same-snapshot prices/probabilities, future capture/form cutoffs, retrospective imports, read-only restart/idempotency, shared capture-wall highlighting, escaping and both HTTP servers. HTTP tests require localhost socket permission.

## Deployment and rollback

Built locally; **not deployed or restarted**. Deploy the new shared module together with its consumers; deploying the capture wall alone would cause an import error. No observer, card timer, emitter or synchronization changes are required.

From the reviewed checkout on core7070, build and transfer the release:

```sh
tar -cf /tmp/tb-next-five-release.tar scripts/tb_au_snapshot_analysis.py scripts/tb_next_five.py scripts/tb_au_capture_wall.py scripts/tb_wall.py scripts/tb_race_queue_wall.py scripts/tb_today_card.html scripts/tb_race_queue_wall.html
scp /tmp/tb-next-five-release.tar basecamp:/tmp/tb-next-five-release.tar
```

On basecamp, preserve deployed files before installing (use a new backup directory for each deployment):

```sh
cd ~/botlab/totebot
mkdir -p backups/next-five-deploy-20260921
tar -cf backups/next-five-deploy-20260921/before.tar scripts/tb_au_capture_wall.py scripts/tb_wall.py scripts/tb_race_queue_wall.py scripts/tb_today_card.html scripts/tb_race_queue_wall.html
tar -xf /tmp/tb-next-five-release.tar
systemctl --user restart tb-race-queue-wall.service
curl --fail http://127.0.0.1:8792/next-five
```

On core7070, the reviewed checkout already contains the implementation:

```sh
cd ~/botlab/totebot
systemctl --user restart tb-wall.service
curl --fail http://127.0.0.1:8787/next-five
```

Check `/api/next-five` on both hosts during the racing day; 503 is expected when today's card is unavailable. The core7070 8792 proxy forwards the authoritative routes without a proxy change.

The local pre-feature rollback point is `backups/next-five-20260921/pre-change/`, containing exact copies of the five edited pre-existing files, including prior uncommitted work. Restore locally with:

```sh
cp -p backups/next-five-20260921/pre-change/* scripts/
systemctl --user restart tb-wall.service
```

Restore on basecamp with:

```sh
cd ~/botlab/totebot
tar -xf backups/next-five-deploy-20260921/before.tar
systemctl --user restart tb-race-queue-wall.service
```

The new standalone modules can remain unused after rollback. No raw observations, state or decisions need restoring. Do not use `git reset` for this rollback: the checkout already contained substantial unrelated uncommitted work.
