# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] — 2026-10-06

### Changed

- **Releases ship only the versioned bundle**, `apple-calendar-X.Y.Z.mcpb`. The unversioned `apple-calendar.mcpb` is no longer built or published.
- **`build.sh` is shared verbatim across the Apple connectors**, reading names from `manifest.json`. Every repo's build now runs `./test.sh`, validates the manifest, checks the version across `manifest.json`, `pyproject.toml`, `__init__.py`, and `uv.lock`, checks the manifest's tool list against the running server (`.github/check_tools.py`, also used by CI), refuses to overwrite an already-built version without `--force`, and records the bundle's checksum in `dist/SHA1SUMS`. `--skip-tests` skips `./test.sh`.

## [0.1.1] — 2026-10-06

### Changed

- **Permission errors name the exact binary to grant.** Claude Desktop launches extensions through a helper that makes the spawned `uv` — not Claude — responsible for their privacy grants. Errors now print that `uv`'s path (e.g. `~/Library/Application Support/Claude/uv-runtime/uv-0.9.7-darwin-arm64/uv`) ready to paste into System Settings, and explain that enabling Claude is not enough, that one grant covers every Apple connector, and that it must be redone when Claude Desktop updates its `uv`.
- The Calendars-access error includes those steps. It used to say to enable Claude, which has no effect.
- **README:** permissions now consistently point at `uv`, the restart step is consistently "quit Claude (⌘Q) and reopen it", and a new *Using several Apple connectors* section covers installing them one at a time, the shared `uv` grant, re-granting after updates, and verifying each connector.
- **Release flow shared across the Apple connectors.** The [release workflow](.github/workflows/release.yml) and [CI](.github/workflows/ci.yml) are now identical in every repo, with per-repo tests in `./test.sh`. Release notes come from this changelog instead of being generated.
- CI checks that `manifest.json`, `pyproject.toml`, `__init__.py`, and `uv.lock` agree on the version, and compares the manifest's tool list against the running server rather than a regex over its source.

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
