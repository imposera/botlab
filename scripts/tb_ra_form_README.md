# Minimal historical runner baseline

Import saved Racing Australia Recent Form HTML on basecamp:

```
cd ~/botlab/totebot/scripts
python3 tb_ra_form.py /path/to/meeting_recent_form.html
python3 tb_ra_form.py /path/to/meeting_recent_form.html --commit
```

The first command previews; the second imports. No website requests occur. File extensions are optional. Join by page meeting/date, race number and unique normalized horse name to the retained Australian registry. Unmatched or ambiguous runners are skipped.

Base is the arithmetic mean (one decimal) of the latest three available rated race starts strictly before the meeting date. Fewer starts are used when only one or two are available; no rated history stays blank. Trials and jump-outs are excluded. Starts are deduplicated by source race URL. This summarizes historical published handicap ratings; it is not a calculated race performance rating, probability, or cross-jurisdiction calibrated score. It does not adjust for margins, weight, distance, going or the age of the historical start.

The Base column in the Capture Wall expands to show contributing dates/venues/ratings and import time. Imports made after scheduled start are marked retrospective; an old saved page is not proof it was available before the race. Existing Rtg values and automated queue decisions are unchanged.

State: `state/tb_ra_form.json`, with source filename/hash, method version, selected starts and raw history text. Raw HTML is archived under `imports/racing_australia/`. First imported record per meeting/market/runner is retained on repeat imports. State replication supplies the local wall. Run commits only on basecamp. This first release uses the command-line importer; the acceptance-page upload form still imports acceptance ratings only.

Tests: `PYTHONPATH=scripts python3 -m unittest test_tb_ra_form test_tb_au_capture_wall`.

## Persistent runner database

Every recent-form `--commit` also updates `runners` in the same atomic `state/tb_ra_form.json` document. Racing Australia's decoded horsecode identifies a horse across meetings; Betfair selection ID is a fallback when unavailable. Only uniquely matched Australian card entries are imported. All eligible rated historical starts from the page are retained and deduplicated by source racecode. Later meeting imports take precedence over older pages for corrections to the same start; previous corrected values remain in `revisions`. The latest database baseline is recomputed from the latest three distinct dated rated starts across imports. Duplicate file/market/selection imports are no-ops.

Race-specific `days` records remain frozen, so the wall's historical Base column is unchanged by later imports. The current database value is separate from those race-time records and is not automatically used as a past pre-race baseline. The database is updated by recent-form imports, not acceptance-only imports or Betfair results.

Inspect the current database on basecamp with `python3 tb_ra_form.py --database`. Each horse records its name, source horse ID, Betfair selection IDs, current baseline, contributing starts, rated-start count, import provenance and correction history. Continue to run writers on basecamp only; local copies are read-only consumers.

## Import new files button

Capture Wall links to `http://core7070:8792/import-form`. Save Recent Form HTML in `recent_form/` on core7070. The `totebot-recent-form` Syncthing folder sends files to basecamp (receive-only there). On the import page choose Scan new files, inspect counts/errors, then Import reviewed files. New pages of any meeting date are eligible; the date inside the page controls matching. Previously imported hashes are skipped. At most 20 valid pages are staged per preview, 2 MB per file; symlinks and auxiliary asset directories are ignored.

`POST /api/form-import/preview` scans the fixed server folder. `POST /api/form-import/commit` consumes its preview_id. Both require the import page's X-Queue-Token. Preview data is kept in memory for 15 minutes (maximum five pending previews), so changes to a file after preview do not alter reviewed content. Errors are reported per file; successful files in a batch remain committed. Retry scans skip completed files. CLI imports continue to work.

## Recent-win Base arrow

A green Base disclosure marker means a win among the last three available actual race starts strictly before the imported meeting date. Unrated races count; trials/jump-outs do not. Unknown finishing positions remain unknown. The expanded cell lists winning start dates/venues and distances. A runner with no numeric baseline can still have a green arrow beside — if a qualifying win is present. Future recent-form imports retain recent_races/recent_win automatically. Existing records can be enriched by reimporting the exact saved page; only missing recent-race fields are added, preserving baseline and original import time.

## Race fundamentals

Recent-form commits also update the structured fundamentals tables, including unrated historical races. See `tb_racing_fundamentals_README.md` for schema, field limitations and query commands.
