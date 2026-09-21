# Decimal price threshold curve

Source: repository `value_play_odds_table`; first column is the user's morning line, second is the post-time price threshold. Decimal conversion adds one to fractional profit odds.

| Reference decimal odds | Threshold decimal odds |
|---:|---:|
| 2.40 | 3.50 |
| 2.60 | 4.00 |
| 2.80 | 4.00 |
| 3.00 | 4.50 |
| 3.50 | 5.00 |
| 4.00 | 5.50 |
| 4.50 | 6.00 |
| 5.00 | 8.00 |

`tb_price_threshold.threshold()` uses shape-preserving monotone cubic interpolation through these anchors. It is continuous with a continuous first derivative and retains the flat 2.60–2.80 interval. The 8.00+ endpoint is treated as a minimum threshold of 8.00. Values outside reference odds 2.40–5.00 return unknown; no extrapolation is invented. This is a user-defined observation threshold, not an empirically validated probability or value model.

Capture Wall uses unrounded earliest-complete-snapshot market-derived Fair odds as input and displays Target odds alongside it. Handicap-rating Base points must not be passed directly to this function. A future comparison against T30 must label that as a saved pre-start snapshot, not an actual post-time executable price. No emitter or queue behaviour changes.
