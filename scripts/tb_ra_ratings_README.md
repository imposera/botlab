# Racing Australia handicap ratings

The collector reads public final-acceptance Hcp Rating cells. Meetings are discovered from state calendars and matched by date and a small explicit venue alias map. Runner matching requires race number and a unique normalized horse name within the meeting. Weights-page race ordering is not used. Missing, nonnumeric and ambiguous ratings remain unavailable.

Published numeric ratings are frozen on first collection, with source URL, jurisdiction, meeting date, collection timestamp and a pre_race flag. They are separate from Betfair metadata and do not influence arming. Capture Wall displays the published rating and source link; collection after scheduled start is labelled. Ratings are saved in state/tb_ra_ratings.json and a daily archive.

Initial collection on 12 September retrieved 124 Doomben ratings. Racing Australia's human-verification challenge prevented further automated access. Automatic collection is paused, with access_status=verification_required persisted. No timer or service is enabled. Do not switch hosts or identities to evade the challenge. A permitted feed or user-downloaded source pages are required to extend coverage while access remains blocked.

Tests: PYTHONPATH=scripts python3 -m unittest test_tb_ra_ratings test_tb_au_capture_wall

## Manual batch import

Use the Capture Wall's Import Racing Australia pages link, or `http://core7070:8792/import-ra`. Save each acceptance meeting as HTML Only in your normal browser, select up to 20 files (2 MB each; 12 MB UI batch limit), then Preview matches and Import reviewed ratings. Only the authoritative queue wall writes ratings. No website request occurs during import.

Meeting/date are extracted from horse links with Acceptances or FinalFields stage markers. Verification pages, weights pages, mismatched dates/meetings, ambiguous horses and conflicting batch ratings are rejected or skipped. Existing numeric baselines remain unchanged. Previews expire after 15 minutes and require the existing session token to submit. Uploaded HTML is parsed as data and stored by content hash outside the served directories under imports/racing_australia. It is never rendered or executed. Import times are recorded; browser modification times are not trusted as evidence of pre-race capture. Source timestamps use the first server preview time.

Validation: 14 import, rating, viewer and HTTP tests passed, including authentication, idempotence, frozen baselines, conflicts and invalid page types. No actual acceptance page was supplied during setup; the known local acceptance download is a verification challenge and was not imported.
