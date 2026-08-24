"""
Apple Calendar MCP Server
=========================
Exposes Apple Calendar to Claude Desktop via the Model Context Protocol.
Uses Apple's first-class EventKit framework (via PyObjC) for full coverage:
recurring events (with per-occurrence and future-events editing), alarms,
attendee/organizer read-back, availability (free/busy), structured
locations, and time zones.

Permission model
----------------
Calendar access is gated by macOS TCC. On first tool invocation the OS
will prompt the user to grant access; alternatively the user can pre-grant
under System Settings → Privacy & Security → Calendars for the parent
process (Claude Desktop).

Tools provided
--------------
Calendar management
  list_calendars           - All event calendars
  create_calendar          - Create a new calendar
  update_calendar          - Rename or recolor a calendar
  delete_calendar          - Delete a calendar (destructive)

Events — read
  get_stats                - Calendar count, events today / next 7 days, next event
  list_events              - Events in a date range, with filters
  search_events            - Free-text search across title, notes, location
  get_event                - Full detail for one event (attendees, recurrence, alarms)
  get_event_link           - ical:// URL to open the event in Calendar.app
  get_availability         - Merged busy intervals (free/busy) in a range

Events — write
  create_event             - Create with full property set (recurrence, alarms, location)
  update_event             - Update any subset of properties; span controls recurring scope
  delete_event             - Delete an event or occurrence (span controls scope)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

from mcp.server import MCPServer

from . import __version__
from .models import (
    AlarmSpec,
    Availability,
    AvailabilityResult,
    CalendarInfo,
    CalendarResult,
    CalendarStats,
    DeleteResult,
    EventDetail,
    EventResult,
    LocationSpec,
    RecurrenceRule,
    SearchResult,
    Span,
)
from .permissions import PermissionDeniedError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("apple_calendar_mcp")

# ---------------------------------------------------------------------------
# Lazy-initialised store. EventKit bootstrap (and a possible TCC prompt) can
# take a few seconds — we MUST NOT run it at import time, the MCP client
# would time out waiting for the initialize response.
# ---------------------------------------------------------------------------

_store = None  # type: ignore[var-annotated]


# ---------------------------------------------------------------------------
# MCP server app
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "Apple Calendar",
    instructions=(
        "Access to Apple Calendar on this Mac via EventKit. "
        "You can list and manage calendars; list, search, create, update, "
        "and delete events (including recurring events, per-occurrence); "
        "read attendees and organizers; and compute free/busy availability. "
        "Event ids are stable across a recurring series — pass "
        "occurrence_date to target one occurrence, and span to control "
        "whether an edit hits one occurrence or all future ones."
    ),
    version=__version__,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_store():
    """Return the CalendarStore, initialising on first call.

    Re-attempts on every call if init previously failed (the user may have
    granted Calendar access since the last attempt).
    """
    global _store
    if _store is not None:
        return _store
    # Defer the heavy import until first use.
    from .calendars import CalendarStore
    try:
        _store = CalendarStore()
        logger.info("Apple Calendar MCP ready (EventKit).")
        return _store
    except PermissionDeniedError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Could not initialise the Calendar store: {exc}"
        ) from exc


def _parse_iso(s: Optional[str], field: str) -> Optional[datetime]:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field} date: {s!r}. Use ISO-8601 format.") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


# ---------------------------------------------------------------------------
# Tools — calendar management
# ---------------------------------------------------------------------------

@mcp.tool()
def list_calendars() -> list[CalendarInfo]:
    """List every event calendar on this Mac.

    Returns id, title, color, source (account), whether the calendar can be
    modified, and which calendar is the default for new events. Use the
    returned ids with other tools that take a `calendar_id`.
    """
    return _require_store().list_calendars()


@mcp.tool()
def create_calendar(
    title: str,
    color: Optional[str] = None,
    source_name: Optional[str] = None,
) -> CalendarResult:
    """Create a new calendar.

    Args:
        title:        Non-empty calendar title.
        color:        Optional '#RRGGBB' hex color.
        source_name:  Account name to host the calendar (e.g. 'iCloud', 'On My Mac').
                      If unspecified, the first writable source is chosen.
    """
    return _require_store().create_calendar(title=title, color=color, source_name=source_name)


@mcp.tool()
def update_calendar(
    calendar_id: str,
    title: Optional[str] = None,
    color: Optional[str] = None,
) -> CalendarResult:
    """Rename or recolor a calendar.

    Args:
        calendar_id:  Identifier from list_calendars.
        title:        New title; pass null to leave unchanged.
        color:        New '#RRGGBB' color; pass null to leave unchanged.
    """
    if title is None and color is None:
        raise ValueError("Provide at least one of: title, color.")
    return _require_store().update_calendar(calendar_id=calendar_id, title=title, color=color)


@mcp.tool()
def delete_calendar(calendar_id: str) -> CalendarResult:
    """Delete a calendar and all its events.

    DESTRUCTIVE. The returned `deleted_event_count` is an estimate of the
    events (within ±2 years) that lived in the calendar at deletion time.

    Args:
        calendar_id:  Identifier from list_calendars.
    """
    return _require_store().delete_calendar(calendar_id)


# ---------------------------------------------------------------------------
# Tools — events (read)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> CalendarStats:
    """Return aggregate counts: calendar count, events today, events in the next 7 days, and the next upcoming event."""
    return _require_store().get_stats()


@mcp.tool()
def list_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    calendar_ids: Optional[list[str]] = None,
    text: Optional[str] = None,
    all_day: Optional[bool] = None,
    include_canceled: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """List event occurrences in a date range, sorted by start time.

    Occurrences of recurring events appear individually; each carries the
    series id plus an `occurrence_date` identifying that instance.

    Args:
        start:            ISO-8601 range start. Default: today at 00:00 local.
        end:              ISO-8601 range end. Default: 30 days after start.
        calendar_ids:     Restrict to these calendar ids. Empty/null = all calendars.
        text:             Substring match on title, notes, or location.
        all_day:          If true, only all-day events. If false, only timed events.
        include_canceled: Include events with canceled status (default false).
        limit:            Max results per page (default 100, max 1000).
        offset:           Pagination offset.
    """
    store = _require_store()
    limit = max(1, min(int(limit), 1000))
    start_dt = _parse_iso(start, "start") or datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_dt = _parse_iso(end, "end") or (start_dt + timedelta(days=30))
    if end_dt <= start_dt:
        raise ValueError("`end` must be after `start`.")
    total, rows = store.list_events(
        start=start_dt,
        end=end_dt,
        calendar_ids=calendar_ids,
        text=text,
        all_day=all_day,
        include_canceled=include_canceled,
        limit=limit,
        offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, events=rows)


@mcp.tool()
def search_events(
    query: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    calendar_ids: Optional[list[str]] = None,
    limit: int = 100,
    offset: int = 0,
) -> SearchResult:
    """Search events by free text across title, notes, and location.

    Args:
        query:        Required substring; case-insensitive.
        start:        ISO-8601 range start. Default: 365 days ago.
        end:          ISO-8601 range end. Default: 365 days from now.
        calendar_ids: Restrict to these calendar ids; null = all calendars.
        limit:        Max results (default 100, max 1000).
        offset:       Pagination offset.
    """
    if not query or not query.strip():
        raise ValueError("`query` must be a non-empty string.")
    store = _require_store()
    limit = max(1, min(int(limit), 1000))
    now = datetime.now().astimezone()
    start_dt = _parse_iso(start, "start") or (now - timedelta(days=365))
    end_dt = _parse_iso(end, "end") or (now + timedelta(days=365))
    if end_dt <= start_dt:
        raise ValueError("`end` must be after `start`.")
    total, rows = store.list_events(
        start=start_dt,
        end=end_dt,
        calendar_ids=calendar_ids,
        text=query.strip(),
        limit=limit,
        offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, events=rows)


@mcp.tool()
def get_event(
    event_id: str,
    occurrence_date: Optional[str] = None,
) -> EventDetail:
    """Fetch a single event with all properties (notes, organizer, attendees, recurrence, alarms, location, time zone).

    Args:
        event_id:        Identifier from list_events / search_events.
        occurrence_date: ISO-8601 occurrence date, to target one occurrence of
                         a recurring series. Omit for non-recurring events (or
                         to get the series master).
    """
    detail = _require_store().get_event(
        event_id, _parse_iso(occurrence_date, "occurrence_date")
    )
    if detail is None:
        raise ValueError(f"Event not found: {event_id}")
    return detail


@mcp.tool()
def get_event_link(
    event_id: str,
    occurrence_date: Optional[str] = None,
) -> dict:
    """Return an ical:// URL that opens the event in Calendar.app (best effort).

    Args:
        event_id:        Identifier from list_events / search_events.
        occurrence_date: ISO-8601 occurrence date for recurring events.
    """
    link = _require_store().get_event_link(
        event_id, _parse_iso(occurrence_date, "occurrence_date")
    )
    return {"event_id": event_id, "event_link": link}


@mcp.tool()
def get_availability(
    start: str,
    end: str,
    calendar_ids: Optional[list[str]] = None,
    include_all_day: bool = False,
    include_tentative: bool = True,
) -> AvailabilityResult:
    """Compute merged busy intervals (free/busy) within a date range.

    Useful for finding open slots: any gap between the returned busy
    intervals is free. Events marked 'free', canceled events, and events the
    user has declined never block time.

    Args:
        start:             ISO-8601 range start (required).
        end:               ISO-8601 range end (required).
        calendar_ids:      Restrict to these calendars; null = all calendars.
        include_all_day:   Treat all-day events as busy (default false).
        include_tentative: Treat tentative events as busy (default true).
    """
    start_dt = _parse_iso(start, "start")
    end_dt = _parse_iso(end, "end")
    assert start_dt is not None and end_dt is not None
    if end_dt <= start_dt:
        raise ValueError("`end` must be after `start`.")
    return _require_store().get_availability(
        start=start_dt,
        end=end_dt,
        calendar_ids=calendar_ids,
        include_all_day=include_all_day,
        include_tentative=include_tentative,
    )


# ---------------------------------------------------------------------------
# Tools — events (write)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_event(
    title: str,
    start_date: str,
    end_date: Optional[str] = None,
    all_day: bool = False,
    calendar_id: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    location: Optional[str] = None,
    structured_location: Optional[LocationSpec] = None,
    availability: Optional[Availability] = None,
    time_zone: Optional[str] = None,
    recurrence: Optional[RecurrenceRule] = None,
    alarms: Optional[list[AlarmSpec]] = None,
) -> EventResult:
    """Create a new calendar event.

    Note: attendees cannot be added programmatically — EventKit's public API
    exposes attendees read-only. Create the event, then invite people from
    Calendar.app if needed.

    Args:
        title:               Non-empty title.
        start_date:          ISO-8601 start. For all_day events the time part is ignored.
        end_date:            ISO-8601 end. Defaults: start + 1 hour (timed), or
                             end of the start day (all-day). All-day end dates
                             are inclusive (last day of the event).
        all_day:             When true, the event is an all-day event.
        calendar_id:         Target calendar id. If null, uses the default calendar.
        notes:               Optional body text.
        url:                 Optional URL to attach.
        location:            Optional free-text location.
        structured_location: Optional geocoded location (title + lat/lon + radius).
        availability:        'busy' | 'free' | 'tentative' | 'unavailable'.
        time_zone:           IANA time zone name for a floating/fixed-zone event
                             (e.g. 'America/New_York').
        recurrence:          Optional RecurrenceRule (frequency + interval +
                             termination + by-rules, e.g. days_of_week).
        alarms:              Optional list of AlarmSpec (relative offset from
                             start, negative = before; or absolute date).
    """
    if not title or not title.strip():
        raise ValueError("`title` must be a non-empty string.")
    start_dt = _parse_iso(start_date, "start_date")
    assert start_dt is not None
    detail = _require_store().create_event(
        title=title,
        start_date=start_dt,
        end_date=_parse_iso(end_date, "end_date"),
        all_day=all_day,
        calendar_id=calendar_id,
        notes=notes,
        url=url,
        location=location,
        structured_location=structured_location,
        availability=availability,
        time_zone=time_zone,
        recurrence=recurrence,
        alarms=alarms,
    )
    return EventResult(event=detail, success=True)


@mcp.tool()
def update_event(
    event_id: str,
    occurrence_date: Optional[str] = None,
    span: Span = "this_event",
    title: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    all_day: Optional[bool] = None,
    calendar_id: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    location: Optional[str] = None,
    structured_location: Optional[LocationSpec] = None,
    availability: Optional[Availability] = None,
    time_zone: Optional[str] = None,
    recurrence: Optional[RecurrenceRule] = None,
    alarms: Optional[list[AlarmSpec]] = None,
    clear_notes: bool = False,
    clear_url: bool = False,
    clear_location: bool = False,
    clear_recurrence: bool = False,
    clear_alarms: bool = False,
) -> EventResult:
    """Update any subset of an event's properties.

    Optional fields default to leaving the existing value untouched. Use the
    `clear_*` flags to explicitly remove a value (since passing null cannot
    distinguish "omit" from "set to null" in MCP tool calls).

    For recurring events: pass `occurrence_date` to pick which occurrence you
    are editing, and `span` to control the scope — 'this_event' edits only
    that occurrence (detaching it), 'future_events' edits it and everything
    after it.

    Args:
        event_id:            Identifier of the event (stable across a series).
        occurrence_date:     ISO-8601 date identifying one occurrence of a
                             recurring series (from list_events).
        span:                'this_event' (default) or 'future_events'.
        title:               New title (cannot be empty).
        start_date:          New ISO-8601 start. If it passes the current end and
                             no end_date is given, the duration is preserved.
        end_date:            New ISO-8601 end (must be after the start).
        all_day:             Convert to/from an all-day event.
        calendar_id:         Move to a different calendar.
        notes:               New notes; or set clear_notes=true to remove.
        url:                 New URL; or set clear_url=true to remove.
        location:            New free-text location; or clear_location=true to remove.
        structured_location: New geocoded location.
        availability:        'busy' | 'free' | 'tentative' | 'unavailable'.
        time_zone:           New IANA time zone name.
        recurrence:          New recurrence rule; or clear_recurrence=true to remove.
        alarms:              Replacement list of alarms; or clear_alarms=true to remove.
        clear_*:             Explicit removal flags.
    """
    detail = _require_store().update_event(
        event_id=event_id,
        occurrence_date=_parse_iso(occurrence_date, "occurrence_date"),
        span=span,
        title=title,
        start_date=_parse_iso(start_date, "start_date"),
        end_date=_parse_iso(end_date, "end_date"),
        all_day=all_day,
        calendar_id=calendar_id,
        notes=notes,
        url=url,
        location=location,
        structured_location=structured_location,
        availability=availability,
        time_zone=time_zone,
        recurrence=recurrence,
        alarms=alarms,
        clear_notes=clear_notes,
        clear_url=clear_url,
        clear_location=clear_location,
        clear_recurrence=clear_recurrence,
        clear_alarms=clear_alarms,
    )
    return EventResult(event=detail, success=True)


@mcp.tool()
def delete_event(
    event_id: str,
    occurrence_date: Optional[str] = None,
    span: Span = "this_event",
) -> DeleteResult:
    """Delete an event, or one/some occurrences of a recurring event.

    Args:
        event_id:        Identifier of the event.
        occurrence_date: ISO-8601 date identifying one occurrence of a
                         recurring series. Omit for non-recurring events —
                         but note that for a recurring event with no
                         occurrence_date, span='this_event' deletes only the
                         first occurrence.
        span:            'this_event' (default) deletes just the targeted
                         occurrence; 'future_events' deletes it and all
                         later occurrences.
    """
    return _require_store().delete_event(
        event_id=event_id,
        occurrence_date=_parse_iso(occurrence_date, "occurrence_date"),
        span=span,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
