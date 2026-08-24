"""Pydantic models for Apple Calendar MCP server."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Calendars (EKCalendar of type Event)
# ---------------------------------------------------------------------------

class CalendarInfo(BaseModel):
    id: str                                   # EKCalendar.calendarIdentifier
    title: str
    color: Optional[str] = None               # "#RRGGBB"
    source_name: str                          # account name (e.g. "iCloud", "Google")
    source_type: str                          # "local" | "calDAV" | "exchange" | "subscribed" | "birthday" | "mobileMe"
    allows_modification: bool = True
    is_default: bool = False                  # default calendar for new events
    is_subscribed: bool = False


# ---------------------------------------------------------------------------
# Recurrence and alarms
# ---------------------------------------------------------------------------

Frequency = Literal["daily", "weekly", "monthly", "yearly"]
DayOfWeek = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class RecurrenceRule(BaseModel):
    """Represents an EKRecurrenceRule for an event."""
    frequency: Frequency
    interval: int = Field(default=1, ge=1)
    end_date: Optional[datetime] = None       # mutually exclusive with end_count
    end_count: Optional[int] = Field(default=None, ge=1)
    days_of_week: Optional[list[DayOfWeek]] = None
    days_of_month: Optional[list[int]] = None  # 1..31, or -1..-31 from end of month
    months_of_year: Optional[list[int]] = None  # 1..12
    set_positions: Optional[list[int]] = None  # e.g. [1] + days_of_week=["monday"] = 1st Monday


class LocationSpec(BaseModel):
    """A structured (geocoded) location for an event."""
    title: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_meters: Optional[float] = None     # None = system default


AlarmKind = Literal["relative", "absolute"]


class AlarmSpec(BaseModel):
    """An EKAlarm, specified relative to the event start OR at an absolute date."""
    kind: AlarmKind
    relative_offset_seconds: Optional[float] = None  # negative = before start
    absolute_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Attendees / organizer
# ---------------------------------------------------------------------------

ParticipantStatus = Literal[
    "unknown", "pending", "accepted", "declined", "tentative",
    "delegated", "completed", "in-process",
]
ParticipantRole = Literal["unknown", "required", "optional", "chair", "non-participant"]


class Participant(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    status: ParticipantStatus = "unknown"
    role: ParticipantRole = "unknown"
    is_current_user: bool = False
    is_organizer: bool = False


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

EventStatus = Literal["none", "confirmed", "tentative", "canceled"]
Availability = Literal["not-supported", "busy", "free", "tentative", "unavailable"]
Span = Literal["this_event", "future_events"]


class EventSummary(BaseModel):
    id: str                                   # EKEvent.eventIdentifier (stable per series)
    calendar_id: str
    calendar_title: str
    title: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    all_day: bool = False
    location: Optional[str] = None
    status: EventStatus = "none"
    availability: Availability = "busy"
    has_recurrence: bool = False
    is_detached: bool = False                 # a modified occurrence of a recurring event
    occurrence_date: Optional[datetime] = None  # identifies this occurrence within a series
    has_attendees: bool = False
    event_link: Optional[str] = None          # ical:// deep link (best effort)


class EventDetail(EventSummary):
    """Full event with notes, organizer, attendees, recurrence, alarms, and timestamps."""
    notes: Optional[str] = None
    url: Optional[str] = None
    time_zone: Optional[str] = None           # IANA name, e.g. "America/Los_Angeles"
    organizer: Optional[Participant] = None
    attendees: list[Participant] = []
    recurrence: Optional[RecurrenceRule] = None
    alarms: list[AlarmSpec] = []
    structured_location: Optional[LocationSpec] = None
    creation_date: Optional[datetime] = None
    modification_date: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    events: list[EventSummary]


class CalendarStats(BaseModel):
    calendar_count: int
    events_today: int
    events_next_7_days: int
    next_event: Optional[EventSummary] = None


class BusyInterval(BaseModel):
    start: datetime
    end: datetime
    event_ids: list[str] = []
    titles: list[str] = []


class AvailabilityResult(BaseModel):
    start: datetime
    end: datetime
    busy: list[BusyInterval]


class CalendarResult(BaseModel):
    """Returned by create/update/delete calendar operations."""
    calendar: Optional[CalendarInfo] = None
    success: bool
    deleted_event_count: Optional[int] = None  # populated only by delete_calendar


class EventResult(BaseModel):
    """Returned by create/update operations."""
    event: EventDetail
    success: bool


class DeleteResult(BaseModel):
    id: str
    success: bool
    span: Span = "this_event"
