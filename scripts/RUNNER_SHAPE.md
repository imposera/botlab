# Runner shape first pass

`tb_runner_shape.py` shares directional symbols across the wall, Blackbook and
historical shape reviewer, and descriptive classification across the wall,
race review and shape reviewer. It accepts `t15`, `T15` and `T-15` stage aliases.
Symbols occupy four fixed intervals: T15→T10→T5→T2→T30 (30 seconds).
`?` means an interval lacks an endpoint. Classes describe the available points;
missing intervals remain unknown even when the available points consistently firm.

Run from the scripts directory:

```bash
python3 tb_shape_review.py --top 20
python3 tb_shape_review.py --shape '▼▼▲▼' --details 20
python3 tb_shape_review.py --output /tmp/tb-shapes.json
```

The optional atomic JSON export uses `totebot.shape_review.v2` and includes both
aggregate `shapes` and individual `runners`. Aggregates separate stage coverage.
Runner records contain analysis version, market/runner identity, prices, capture
timestamps, price sources, coverage, missing stages, quality flags, descriptive
class, observed reversal count, slopes and fitted curvature. `timing_basis` is
scheduled; `slope_time_basis` separately identifies captured versus nominal times.

Slopes use strictly increasing, timezone-aware capture timestamps when all are
available. Missing timestamps use nominal stage offsets with a quality flag.
Complete but non-increasing timestamps disable slopes and fitting. Field-wide
scratching breaks suppress movement, slopes, reversal counts and fits. Delayed
feeds and mixed price sources are flagged. Capture time does not establish the
age of the last trade. Fits remain descriptive, not predictive confidence.

REV% now counts reversals in observed available prices, separately from fitted
CURVE. Existing derived Blackbook/review files need rebuilding to reflect shared
symbol changes. Include `tb_runner_shape.py` when deploying its consumers.

This pass reads scheduled history only. Rolling/observed-start adapters, charts,
outcome comparisons and service deployment are subsequent work. Raw history is
not modified; export occurs only with `--output`.

Validation:

```bash
python3 -m unittest test_tb_runner_shape test_tb_wall test_tb_review test_tb_blackbook test_tb_blackbook_wall test_tb_race_observer
```
