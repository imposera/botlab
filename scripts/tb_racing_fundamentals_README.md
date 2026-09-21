# Racing fundamentals database — first release

Stored under `fundamentals` in the existing atomic `state/tb_ra_form.json` database. Only basecamp imports write it. Both historical rated and unrated actual race starts are extracted from saved Recent Form HTML; trials, jump-outs and starts on/after the source meeting date are excluded. Existing frozen race baselines and the mean-of-three rating formula are preserved. There is no change to queue or emitter behaviour.

Tables (JSON dictionaries):
- `horses`: Racing Australia horse ID (Betfair selection ID fallback), name and selection IDs.
- `races`: current Australian card race context (country, full track name, date, number, market name and start time); historical RA race IDs with per-source observations.
- `starts`: horse/race pairs with date, track abbreviation, raw race class, distance metres, going and going rating, synthetic surface when explicit, position/field size, assigned and claimed carried weight, barrier, handicap rating and margin lengths when supplied.
- `imports`: source hash, filename, meeting date, original import time/pre-race flag, parser version and linked market/runner IDs.
- `revisions`: previous start records retained when replaced by a newer source.

Historical track abbreviations are preserved without guessed name expansion. Historical country is unknown unless explicitly supported; AU describes the imported current meeting, not necessarily each prior start. Turf is not inferred solely from a going grade. Race class remains the source's text rather than a fabricated cross-jurisdiction scale. Missing fields are null; DNF/unknown positions retain source text. Raw start text and source URLs remain alongside parsed fields. This first release does not use the fundamentals to change baseline scores.

Every committed Recent Form import updates these tables in the same locked atomic write as runner baselines. Exact file/market/runner repeats are idempotent. Earlier meeting pages cannot replace a newer source's historical start. Historical race assertions remain linked to their individual source imports. Backfilling exact known pages retains original baseline and import metadata.

Inspect on basecamp:
```
cd ~/botlab/totebot/scripts
python3 tb_racing_fundamentals.py
python3 tb_racing_fundamentals.py --horse 'Horse name'
```

The first command reports coverage, including unknown fields. The second returns matching horses and their structured starts. Database state replicates to core7070 for read access. Existing Capture Wall Base behaviour is unchanged; there is no new database browsing panel in this release.

Tests: `PYTHONPATH=scripts python3 -m unittest test_tb_racing_fundamentals test_tb_ra_form test_tb_ra_form_import`.
