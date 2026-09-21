We are adding a new feature to ToteBot:

FEATURE
Observe a table showing the next five races in Australia and the Top 3 shape qualifing runners.


PURPOSE
This replaces the time consuming process of finding and race with price shape of interest
as well as reducing missed opertunites.

CURRENT SYSTEM
The working project is:
~/botlab/totebot

Before changing anything:
1. Read the relevant README/roadmap.
2. Trace the existing data flow from collector → state/artifacts → calculation → wall.
3. Inspect the actual deployed/current files; do not reconstruct them from memory.
4. Identify the authoritative writer and any read-only replicas.
5. Check git status and preserve unrelated work.

REQUIRED BEHAVIOUR
A Table of the next five races in Australia.
Learn and classify price shapes from existing collector data flow.
The next five table should should summarize the top three shape qualifing runners with the
snapshot price, drift then recovery, staedy firm, Firm then drift etc
Present the race card and calculated columns per the Australian race card,
remove the Top winner profiles from the capture wall but highlight the qualifiy runners

Missing data must remain visibly missing.
Preserve actual capture timestamps and delayed-feed indicators.
Do not mix runner observations from different snapshot times in market comparisons.
Treat scratchings as a break in comparable price history.

DATA
Inputs:
Betfair API (adjust snapshot intervals as necassary to learn race shapes respecting
available data rates and delayed data limitions.
Race from per Austral, daily recent form manual uploads

Outputs:
Next five races wall

Race Name
	Horse Name		Base	Chance	Fair Odds	Move 	class 					Latest

Grafton race 7
	Star of Winston	55		15%		6.29		-5.6%	Drift then recovery		5.1 - T2
	....
	....



Historical calculations must use only information available before the race being assessed. Avoid look-ahead leakage.

INTERFACE
Keep the existing ToteBot visual style and terminology.
Explain percentages and classifications in plain language.

IMPLEMENTATION
Prefer extending existing shared modules over duplicating logic.
Keep collection, derived analysis, presentation, and decisions separate.
Use atomic state writes and existing locks where applicable.
Maintain backward compatibility with older/missing artifacts.

VALIDATION
Add focused tests for:
- normal complete data
- missing snapshots/fields
- scratching
- delayed feed
- stale or corrupt state
- restart/idempotency where relevant
- historical cutoff/no future leakage

Run the relevant existing test suite as well as the new tests.

DELIVERY
First report your understanding and proposed file-level design.
Then implement unless you find a material ambiguity or safety issue.
After implementation provide:
- files changed
- exact behaviour added
- tests run and results
- deployment/restart commands
- rollback point
- anything deliberately not implemented
