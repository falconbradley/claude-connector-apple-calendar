# Apple Calendar MCP

A Claude Desktop extension that gives **Claude full access to Apple Calendar** on macOS via Apple's first-class **EventKit** framework. Read, search, create, update, and delete events — including recurring events (edited per-occurrence or for all future occurrences), alarms, structured locations, attendee read-back, and free/busy availability.

Packaged as an [MCPB desktop extension](https://support.claude.com/en/articles/12922929-building-desktop-extensions-with-mcpb) with the Calendar.app icon and one-click install.

Companion to the [Apple Mail](https://github.com/falconbradley/claude-connector-apple-mail), [Apple Reminders](https://github.com/falconbradley/claude-connector-apple-reminders), [Apple Notes](https://github.com/falconbradley/claude-connector-apple-notes), and [Apple Messages](https://github.com/falconbradley/claude-connector-apple-messages) connectors.

---

## What it does

| Tool | Description |
|------|-------------|
| `list_calendars` | Every calendar with id, title, color, source/account, write-access flag, and which is the default |
| `create_calendar` | Create a new calendar (optional color and source) |
| `update_calendar` | Rename or recolor an existing calendar |
| `delete_calendar` | Delete a calendar and all its events (destructive) |
| `get_stats` | Calendar count, events today, events in the next 7 days, next upcoming event |
| `list_events` | Event occurrences in a date range with filters: calendars, text, all-day, canceled |
| `search_events` | Free-text search across title, notes, and location, within an optional date range |
| `get_event` | Full detail — notes, URL, organizer, attendees + their responses, recurrence, alarms, location, time zone |
| `get_event_link` | `ical://` URL to open the event in Calendar.app |
| `get_availability` | Merged busy intervals (free/busy) over a range — the gaps are your open slots |
| `create_event` | Create with full property set: recurrence, alarms, structured location, availability, time zone |
| `update_event` | Update any subset of properties; `occurrence_date` + `span` control recurring scope; `clear_*` flags remove fields |
| `delete_event` | Delete an event or occurrence(s) of a recurring series (span-aware) |

## How it works

Communication with Calendar happens through **EventKit** (`EKEventStore`, `EKEvent`, `EKCalendar`, `EKRecurrenceRule`, `EKAlarm`, `EKStructuredLocation`) via PyObjC — the same first-class framework Calendar.app itself uses, reading the same local store (including iCloud-, Google-, and Exchange-synced calendars).

This is the same approach as the [companion Apple Reminders connector](https://github.com/falconbradley/claude-connector-apple-reminders), and differs from the AppleScript/JXA route: EventKit exposes the full data model (recurrence read-back, per-occurrence editing, attendee responses, structured locations) and is dramatically faster. Unlike Reminders, event fetches are synchronous in EventKit, so no async bridging is needed; the one quirk is that the events predicate spans at most ~4 years per query, so wider ranges are chunked transparently.

### Recurring events

Occurrences of a recurring event share one stable `event_id`; each occurrence returned by `list_events` also carries an `occurrence_date`. Write tools accept both:

- `occurrence_date` picks **which** occurrence you're targeting
- `span` picks the **scope**: `this_event` (just that occurrence, detaching it) or `future_events` (that occurrence and everything after)

---

## Requirements

- macOS 14 Sonoma or later (`requestFullAccessToEvents` was added in 14)
- Python 3.11+
- Claude Desktop with extension support
- Calendar permission — **Full Access** — granted to Claude Desktop (see below)

---

## Installation

### Option 1: Desktop Extension (recommended)

Download the latest `.mcpb` from [Releases](../../releases), then **double-click** to install.

Or build from source:

```bash
git clone https://github.com/falconbradley/claude-connector-apple-calendar.git
cd claude-connector-apple-calendar
./build.sh
```

Then double-click `dist/apple-calendar.mcpb` (or drag it into Claude Desktop).

The extension appears in **Settings > Extensions** with the Calendar icon.

### Option 2: Manual MCP config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-calendar": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/claude-connector-apple-calendar", "apple-calendar-mcp"]
    }
  }
}
```

### Permissions

The first time Claude calls a Calendar tool, macOS will prompt you to grant **Calendars** access. Choose **Full Access** — the macOS 14+ "Add Only" grant can't read events back, so every read tool (and the read-back after each write) would fail.

If the prompt doesn't appear (which can happen with unsigned interpreters launched as child processes):

1. Open **System Settings → Privacy & Security → Calendars**
2. Add Claude Desktop (or whichever process is running `uv`) and enable **Full Access**
3. Quit and relaunch Claude Desktop

To verify access status, run:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT client, auth_value FROM access WHERE service='kTCCServiceCalendar'"
```

(`auth_value` of `2` = full access, `0` = denied.)

---

## Usage examples

Once installed, just ask Claude naturally:

- *"What's on my calendar today?"*
- *"When am I free for an hour tomorrow afternoon?"*
- *"Schedule lunch with Sam on Friday at noon at Zuni Café."*
- *"Move my 3pm to 4:30 and make it 45 minutes."*
- *"Set up a weekly team sync every Tuesday at 10am for the next 8 weeks."*
- *"Cancel just next Monday's standup, keep the rest of the series."*
- *"Add a 10-minute-before alert to my dentist appointment."*
- *"Who hasn't responded to the offsite invite?"*
- *"Create a 'Travel' calendar and put my flight itinerary on it."*

---

## Building from source

```bash
# Install mcpb CLI (one time)
npm install -g @anthropic-ai/mcpb

# Install uv (one time, if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Build the extension
./build.sh

# Or manually:
mcpb validate manifest.json
mcpb pack . dist/apple-calendar.mcpb
```

### Project layout

```
claude-connector-apple-calendar/
├── manifest.json                    # MCPB desktop extension manifest
├── icon.png                         # Calendar.app icon (512x512)
├── icons/                           # Multi-size icons
│   ├── icon-128.png
│   ├── icon-256.png
│   └── icon-512.png
├── pyproject.toml                   # Python package + dependencies
├── build.sh                         # Validate + pack build script (gates on version drift)
├── tools/                           # Dev-only icon extraction (not packed)
├── tests/
│   └── test_e2e.py                  # End-to-end tests
└── src/
    └── apple_calendar_mcp/
        ├── __init__.py
        ├── server.py                # MCP tools (MCPServer)
        ├── calendars.py             # EventKit-backed CalendarStore
        ├── permissions.py           # TCC grant helpers
        └── models.py                # Pydantic data models
```

### Tests

```bash
# Static tests (model shapes, validation, helpers — no permission needed)
uv run python tests/test_e2e.py --skip-live

# Full suite (requires Calendar permission)
uv run python tests/test_e2e.py
```

Live tests operate against a dedicated `__claude_mcp_test__` calendar which is created at setup and torn down at the end.

---

## Security & privacy

- All data stays on your Mac — this is a local MCP server. EventKit reads from the same store Calendar.app uses, including iCloud/Google/Exchange-synced calendars.
- Operations gated by macOS TCC: nothing happens until you grant Calendar access.
- macOS-only (`"platforms": ["darwin"]` in manifest).
- The connector never reaches outside the Calendar entity — no Reminders, contacts, or files.
- Destructive operations (`delete_calendar`, `delete_event`) are explicit tools the model must choose to call; they don't run as side effects of reads.
- Creating or modifying events never emails invitations — EventKit's public API can't add attendees, so nothing leaves your machine.

---

## Troubleshooting

**"Apple Calendar access was not granted"**
Open **System Settings → Privacy & Security → Calendars**, ensure Claude Desktop is listed with **Full Access** (not "Add Only"), then restart Claude Desktop.

**Permission prompt doesn't appear**
Unsigned Python interpreters launched by Claude Desktop sometimes don't trigger the prompt automatically. Add Claude Desktop manually under **System Settings → Privacy & Security → Calendars**.

**Events beyond ~4 years out don't appear**
EventKit's events predicate spans at most 4 years per query. The connector chunks wider ranges automatically, but extremely wide ranges will be slow — prefer narrower windows.

**A recurring-event edit changed the whole series**
Pass `occurrence_date` (from `list_events`) to target one occurrence, and keep `span='this_event'`. `span='future_events'` intentionally rewrites that occurrence and everything after it.

**Can Claude invite people to a meeting?**
No — EventKit's public API exposes attendees read-only. Claude can read who's invited and how they responded, but you add invitees in Calendar.app.

**Extension doesn't appear after install**
Make sure you're running a recent Claude Desktop that supports MCPB extensions. Restart Claude Desktop after installing.

---

## Roadmap

**v1 — shipped**
- [x] List, create, rename, delete calendars
- [x] List, search, get events with filters and pagination
- [x] Create, update, delete events (span-aware for recurring series)
- [x] Recurrence (daily/weekly/monthly/yearly with intervals, by-day, by-month, set positions, end conditions)
- [x] Alarms (relative, absolute)
- [x] Structured (geocoded) locations
- [x] Attendee and organizer read-back with response status
- [x] Free/busy availability with interval merging
- [x] `ical://` deep links

**v2 — under consideration**
- [ ] RSVP / respond-to-invite (blocked on public EventKit API)
- [ ] Travel-time and conference-URL read-back where EventKit exposes them
- [ ] Bulk operations (move/delete multiple events)
- [ ] Natural "find a slot for N people" helper on top of get_availability

---

## License

[MIT](LICENSE)
