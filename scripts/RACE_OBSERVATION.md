# Delayed-race observation release

The emitter now uses market observations to hold an armed target through a delay.
The companion `tb_race_observer.py` supplies those observations independently of
the existing scheduled capture service. It calls only `listMarketBook`; it does
not place bets. Scheduled timestamps, arm IDs and scheduled history files stay
intact. Existing initial-arm consumers still use the positive scheduled countdown.

## Components

- Emitter v1.2.1: retains a target while observations support a bounded hold;
  prevents automatic/manual rearming during the post-scheduled hold under the
  existing hard-lock behavior; releases on observed start, closure or timeout.
- Observer v1.0.0: samples the armed market every 10 seconds by default; stores a
  rolling 20-minute pre-play buffer; records suspensions, removals and observed start.
- Wall v1.5.0: displays delayed/suspended/unknown/start-observed states and a
  separate expandable rolling-price table with sample ages. Scheduled volume
  comparisons retain their original time basis.
- Review v1.2.0 and Blackbook v1.3.0: flag price paths crossing a runner removal;
  do not classify a reduction-factor adjustment as ordinary net firming/drifting.
  Previously saved derived reviews/registers need rebuilding to acquire this flag.

## Deployment

The supplied `betfair_gateway.py` is now integrated. Its login and
`betting_api(operation, payload, session_token)` contracts have been checked with
mocked HTTP. Gateway requests no longer print polling payloads. If the deployed
`tb_names.py` helper is absent, the gateway uses the bundled
`tb_liquidity.clean_runner_name` cloth-prefix normalizer.

Run services on **Basecamp**, where the gateway expects its credentials and
certificates. Adding the gateway source to the workstation does not transfer
those credentials. No live service has been installed or activated here.

Required deployment files include the updated scripts, `betfair_gateway.py`,
`tb_betfair_preflight.py`, the `tb_race_*` modules, `tb_blackbook.py`,
`tb_liquidity.py`, and the supplied **`manual_arm_request.py`** used by the emitter.
Both the gateway and manual-arm helper are now present in this checkout. Install `requests` for the service interpreter (`/usr/bin/python3`
in these units). On Debian/Ubuntu, `sudo apt-get install python3-requests` provides
that package. `requirements-betfair.txt` is supplied for virtualenv deployments;
if using a virtualenv, override both ExecStart and ExecStartPre to its interpreter.

Three user units are provided:

| Unit | Behavior |
| --- | --- |
| `tb-race-observer.service` | Continuous rolling observer, restart on failure |
| `tb-betfair-emitter.service` | One emitter run; requires the observer service |
| `tb-betfair-emitter.timer` | Runs the emitter 30 seconds after each completed run |

All use the same optional configuration file:
`~/.config/totebot/betfair-services.env`. Its example contains paths only:

```ini
PYTHONPATH=/opt/betfair
BETFAIR_SECRETS_FILE=/opt/betfair/secrets.env
```

Local modules beside the scripts are found automatically; PYTHONPATH supports
existing gateway/manual-arm helpers in `/opt/betfair`. Change it if that directory
is different. Both the emitter and companion support `--secrets-file`; the gateway
CLI also honors `BETFAIR_SECRETS_FILE`. Keep credential values in the existing
protected secrets file. It must provide BETFAIR_USERNAME, BETFAIR_PASSWORD,
BETFAIR_API_KEY, and BETFAIR_CERT_PATH. That directory must contain readable
`client-2048.crt` and `client-2048.key` files, as required by the added gateway.

Before activation, run on Basecamp with the same paths/interpreter as the units:

```bash
cd ~/botlab/totebot/scripts
PYTHONPATH=/opt/betfair /usr/bin/python3 tb_betfair_preflight.py --role emitter
systemd-analyze --user verify tb-race-observer.service tb-betfair-emitter.service tb-betfair-emitter.timer
```

Preflight checks module source contracts, the requests package, name-helper
availability and credentials-file readability. It does not execute gateway code,
read credential values, contact Betfair, or prove certificate/API validity.
Each service repeats its preflight before starting.

Install the prepared units:

```bash
mkdir -p ~/.config/systemd/user
cp tb-race-observer.service tb-betfair-emitter.service tb-betfair-emitter.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

Before enabling the new timer, stop/disable the existing emitter timer or cron
entry on Basecamp so only one scheduler owns the target. Its name must be taken
from that host's existing configuration. Keep the scheduled capture and result
services running; their source is not in this checkout, so check that they do not
independently expire or overwrite the target at scheduled start.

Activate the observer first, verify it is active, then enable the emitter timer:

```bash
systemctl --user enable --now tb-race-observer.service
systemctl --user is-active tb-race-observer.service
systemctl --user enable --now tb-betfair-emitter.timer
systemctl --user list-timers tb-betfair-emitter.timer
journalctl --user -u tb-race-observer.service -u tb-betfair-emitter.service -n 30
```

Restart the wall after updating its scripts. If Basecamp captures and another host
serves the wall, include `observations/` in the existing sync alongside `state/`
and `history/`; otherwise the remote wall cannot show the delay and rolling data.
Use the host's existing user-service/linger policy to keep services running after
logout. No linger configuration is changed by this release.

The companion can also run in the foreground:

```bash
PYTHONPATH=/opt/betfair python3 tb_race_observer.py --base-dir ~/botlab/totebot
```

`--once` performs one tick. `--help` and `--version` need no gateway. A lock prevents
two companions from running against the same state directory. The companion
reuses authentication and refreshes its session after API errors.

## Configuration

Optional `config/race_observation.json`:

```json
{
  "poll_seconds": 10,
  "fresh_seconds": 45,
  "stale_hold_seconds": 180,
  "max_delay_seconds": 1800,
  "buffer_seconds": 1200
}
```

Maximum delay is measured from scheduled start and cannot slide forever. Fresh
OPEN/pre-play data continues holding the target; SUSPENDED holds it without
claiming a start. Stale/error data changes the display to unknown, with at most
180 seconds of hold after the later of scheduled start and the last observation.
The maximum-delay limit still applies. No observer data after scheduled start
gets only that bounded fallback. Settings are clamped to documented safe ranges
in `tb_race_lifecycle.policy`; freshness covers at least two polling intervals.

## Data contract and timing limits

Files are atomic JSON under
`observations/YYYY-MM-DD/<market_id>/`:

- `lifecycle.json`: last receipt, state, most recent book, removal events and start source.
- `rolling.json`: timestamped OPEN/pre-play observations within the buffer.
- `observed_start.json`: separately derived T−15/T−10/T−5/T−2/T−30 selections,
  anchored to the first received in-play observation. Each selection is at/before
  its desired offset, within two poll intervals. Missing offsets remain absent.

A suspension is not a start signal. CLOSED without an observed in-play transition
ends observation without fabricating a start time. Markets that never turn in-play
may therefore have no observed-start capture set. The first received in-play
observation is an **estimate**, not an official off time. Polling, transport and
feed delays are retained as uncertainty; delayed-feed flags are preserved.
A service that starts after the race is already in-play may have no usable
pre-race samples. The release does not infer vet/parade/barrier reasons.

No data is written into raw `history/`. Observed-start files are not read by the
scheduled market-volume statistical register. The wall uses elapsed lookbacks
for its rolling view, never a predicted countdown to an unknown jump. Removal
comparisons are suppressed across the break for the entire field. A removal
already present in the first sample is not evidence of a subsequent price break.

Run checks:

```bash
python3 -m unittest test_tb_betfair_deployment test_tb_race_observer test_betfair_t15_emit test_tb_wall test_tb_review test_tb_market_volume test_tb_blackbook test_tb_blackbook_wall
```
