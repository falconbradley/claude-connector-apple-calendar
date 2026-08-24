# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-24

Initial release.

### Added

- **Calendar management**: `list_calendars` (with default-calendar and
  subscribed flags), `create_calendar`, `update_calendar`, `delete_calendar`.
- **Event reads**: `list_events` (date-range occurrence expansion, with
  text / all-day / canceled filters and pagination), `search_events`,
  `get_event` (full detail: notes, URL, organizer, attendees + responses,
  recurrence, alarms, structured location, time zone), `get_event_link`
  (`ical://` deep link), `get_stats`.
- **Free/busy**: `get_availability` — merged busy intervals over a range,
  honouring per-event availability, canceled status, declined invitations,
  and (optionally) all-day and tentative events.
- **Event writes**: `create_event` (recurrence rules, relative/absolute
  alarms, geocoded structured locations, availability, IANA time zones,
  all-day normalisation), `update_event` (partial updates with explicit
  `clear_*` flags; `occurrence_date` + `span` for per-occurrence vs
  future-events edits of recurring series; duration preserved when only the
  start moves), `delete_event` (span-aware).
- Fetches chunk transparently past EventKit's 4-year predicate window.
- CI: static tests on macOS across Python 3.11/3.13, a server-import check,
  manifest validation, and version/tool-list consistency gates.
- Release workflow: on a `v*` tag, tests, verifies tag↔manifest version,
  builds, and publishes the `.mcpb` bundles.

### Known limitations

- Attendees are read-only — EventKit's public API cannot add or invite
  attendees, and cannot RSVP on the user's behalf.
- macOS 14+ "write-only" Calendar access is not sufficient; the connector
  needs Full Access to read events back.
