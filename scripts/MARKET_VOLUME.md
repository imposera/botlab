# Market Volume release

`tb_wall.py` v1.3.0 reads `state/tb_market_volume.json`, built by
`tb_market_volume.py` v1.0.0. Restart the wall process after upgrading.

Build or refresh after syncing completed history:

```bash
python3 tb_market_volume.py scan
python3 tb_market_volume.py scan --dry-run
```

For a different installation, place `--base-dir /path/to/totebot` before `scan`.
The scan atomically replaces only the derived state register. It never changes
history or calls Betfair. Run it again after new completed races arrive; the
wall reloads the file automatically. No scheduled job is installed by this release.

The register stores per-race stage totals plus 60-day per-cohort medians,
quartiles and sample counts. The wall selects the 60 days before the displayed
race (or the current time for a live race) and excludes that market. Historical
views never use later races. Groups require the same normalized track, market
type and currency. Legacy records without a type use WIN, matching this repo's
capture pipeline. Missing currency is a separate, explicitly labeled group;
its unit consistency cannot be verified from the saved data.

The stage bar uses the latest validated T−15/T−10/T−5/T−2 minute or T−30 second
capture. Between stages it keeps that capture, with its age, rather than
comparing an arbitrary countdown to a fixed historical stage. The second bar
compares the latest reported pre-race amount with the historical T−30 median.
Closed totals are not used because they may include in-play trading.

Colors: amber below the historical 25th percentile, blue within the middle
50%, green above the 75th percentile. Below 20 observations, bars are grey and
provisional. The final-reference bar is blue when sufficiently sampled; it does
not classify the live stage as weak or strong. Percentages may exceed 100%.
A zero median cannot produce a percentage. Unknown values are not treated as zero.

Captures require explicit pre-race status, capture/start timestamps, and timing
within 45 seconds of the named stage (15 seconds for T−30). The panel disables
live comparisons at scheduled start or when in-play status is unknown/true.
Register age over 24 hours and capture age over 120 seconds are labeled stale.
These limits are display safeguards, not feed freshness guarantees.

The small trend includes captured totals and stage medians; expandable numeric
values and sample counts accompany it. Text labels accompany colors. Trading
volume is not a measure of currently available order-book liquidity.

Validation:

```bash
python3 -m unittest test_tb_market_volume test_tb_wall test_tb_blackbook_wall test_tb_blackbook
```
