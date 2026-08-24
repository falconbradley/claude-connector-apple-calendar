"""EventKit-backed bridge to Apple Calendar.

Wraps ``EKEventStore`` with a synchronous Python API.  Unlike Reminders,
event fetches (``eventsMatchingPredicate:``) are already synchronous, so no
threading bridge is needed — but the events predicate only spans up to four
years, so wider queries are chunked transparently.

All methods raise ``PermissionDeniedError`` if Calendar access has not
been granted, and ``RuntimeError`` for other EventKit failures.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import quote

from EventKit import (  # type: ignore
    EKAlarm,
    EKCalendar,
    EKEntityTypeEvent,
    EKEvent,
    EKEventStore,
    EKRecurrenceDayOfWeek,
    EKRecurrenceEnd,
    EKRecurrenceRule,
    EKSourceTypeBirthdays,
    EKSourceTypeCalDAV,
    EKSourceTypeExchange,
    EKSourceTypeLocal,
    EKSourceTypeMobileMe,
    EKSourceTypeSubscribed,
    EKStructuredLocation,
)
from CoreLocation import CLLocation  # type: ignore
from Foundation import (  # type: ignore
    NSDate,
    NSTimeZone,
    NSURL,
)

from .models import (
    AlarmSpec,
    Availability,
    AvailabilityResult,
    BusyInterval,
    CalendarInfo,
    CalendarResult,
    CalendarStats,
    DayOfWeek,
    DeleteResult,
    EventDetail,
    EventStatus,
    EventSummary,
    Frequency,
    LocationSpec,
    Participant,
    ParticipantRole,
    ParticipantStatus,
    RecurrenceRule,
    Span,
)
from .permissions import (
    PermissionDeniedError,
    authorization_status_label,
    request_calendar_access,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants and lookup tables
# ---------------------------------------------------------------------------

# EKSpan
EK_SPAN_THIS_EVENT = 0
EK_SPAN_FUTURE_EVENTS = 1

_SPAN_TO_EK = {"this_event": EK_SPAN_THIS_EVENT, "future_events": EK_SPAN_FUTURE_EVENTS}

# EKRecurrenceFrequency
EK_FREQ_DAILY = 0
EK_FREQ_WEEKLY = 1
EK_FREQ_MONTHLY = 2
EK_FREQ_YEARLY = 3

_FREQ_TO_EK = {
    "daily": EK_FREQ_DAILY,
    "weekly": EK_FREQ_WEEKLY,
    "monthly": EK_FREQ_MONTHLY,
    "yearly": EK_FREQ_YEARLY,
}
_EK_TO_FREQ: dict[int, Frequency] = {v: k for k, v in _FREQ_TO_EK.items()}  # type: ignore[misc]

# EKWeekday is 1=Sunday..7=Saturday in EventKit; map to Python-style names.
_DAY_TO_EK = {
    "sunday": 1, "monday": 2, "tuesday": 3, "wednesday": 4,
    "thursday": 5, "friday": 6, "saturday": 7,
}
_EK_TO_DAY: dict[int, DayOfWeek] = {v: k for k, v in _DAY_TO_EK.items()}  # type: ignore[misc]

# EKEventStatus
_EK_TO_STATUS: dict[int, EventStatus] = {
    0: "none", 1: "confirmed", 2: "tentative", 3: "canceled",
}

# EKEventAvailability
_AVAIL_TO_EK = {
    "not-supported": -1, "busy": 0, "free": 1, "tentative": 2, "unavailable": 3,
}
_EK_TO_AVAIL: dict[int, Availability] = {v: k for k, v in _AVAIL_TO_EK.items()}  # type: ignore[misc]

# EKParticipantStatus
_EK_TO_PSTATUS: dict[int, ParticipantStatus] = {
    0: "unknown", 1: "pending", 2: "accepted", 3: "declined",
    4: "tentative", 5: "delegated", 6: "completed", 7: "in-process",
}

# EKParticipantRole
_EK_TO_PROLE: dict[int, ParticipantRole] = {
    0: "unknown", 1: "required", 2: "optional", 3: "chair", 4: "non-participant",
}

_SOURCE_TYPE_LABEL = {
    EKSourceTypeLocal: "local",
    EKSourceTypeExchange: "exchange",
    EKSourceTypeCalDAV: "calDAV",
    EKSourceTypeMobileMe: "mobileMe",
    EKSourceTypeSubscribed: "subscribed",
    EKSourceTypeBirthdays: "birthday",
}

# The events predicate silently returns nothing beyond a 4-year window;
# stay well inside it per chunk.
_MAX_PREDICATE_DAYS = 4 * 365


# ---------------------------------------------------------------------------
# Date / NSDate helpers
# ---------------------------------------------------------------------------

def _ns_date_to_datetime(ns_date: Any) -> Optional[datetime]:
    if ns_date is None:
        return None
    try:
        ts = ns_date.timeIntervalSince1970()
    except Exception:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def _datetime_to_ns_date(dt: Optional[datetime]) -> Optional[Any]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _local_midnight(dt: datetime) -> datetime:
    """Local 00:00:00 of the given moment's day."""
    local = dt.astimezone() if dt.tzinfo else dt
    return datetime.combine(local.date(), time.min).astimezone()


def _local_end_of_day(dt: datetime) -> datetime:
    """Local 23:59:59 of the given moment's day."""
    local = dt.astimezone() if dt.tzinfo else dt
    return datetime.combine(local.date(), time(23, 59, 59)).astimezone()


# ---------------------------------------------------------------------------
# Conversion: EventKit objects ↔ Pydantic models
# ---------------------------------------------------------------------------

def _color_to_hex(color: Any) -> Optional[str]:
    """Convert a CGColorRef to '#RRGGBB' (best effort)."""
    if color is None:
        return None
    try:
        from Quartz import CGColorGetComponents, CGColorGetNumberOfComponents  # type: ignore
        n = CGColorGetNumberOfComponents(color)
        comps = CGColorGetComponents(color)
        if comps is None:
            return None
        if n >= 3:
            r, g, b = comps[0], comps[1], comps[2]
        elif n == 2:
            # Grayscale + alpha
            r = g = b = comps[0]
        else:
            return None
        return "#{:02X}{:02X}{:02X}".format(
            max(0, min(255, int(round(r * 255)))),
            max(0, min(255, int(round(g * 255)))),
            max(0, min(255, int(round(b * 255)))),
        )
    except Exception:
        return None


def _hex_to_cgcolor(hex_str: str) -> Optional[Any]:
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
    except ValueError:
        return None
    try:
        from Quartz import (  # type: ignore
            CGColorCreate,
            CGColorSpaceCreateDeviceRGB,
        )
        cs = CGColorSpaceCreateDeviceRGB()
        return CGColorCreate(cs, [r, g, b, 1.0])
    except Exception:
        return None


def _recurrence_to_model(rule: Any) -> Optional[RecurrenceRule]:
    if rule is None:
        return None
    freq = _EK_TO_FREQ.get(rule.frequency())
    if freq is None:
        return None
    end = rule.recurrenceEnd()
    end_date: Optional[datetime] = None
    end_count: Optional[int] = None
    if end is not None:
        if end.endDate() is not None:
            end_date = _ns_date_to_datetime(end.endDate())
        oc = end.occurrenceCount()
        if oc and oc > 0:
            end_count = int(oc)

    days_of_week: Optional[list[DayOfWeek]] = None
    raw_days = rule.daysOfTheWeek()
    if raw_days:
        days = []
        for d in raw_days:
            day_of_week = d.dayOfTheWeek() if hasattr(d, "dayOfTheWeek") else None
            if day_of_week and day_of_week in _EK_TO_DAY:
                days.append(_EK_TO_DAY[day_of_week])
        days_of_week = days or None

    def _to_int_list(arr: Any) -> Optional[list[int]]:
        if not arr:
            return None
        out = []
        for x in arr:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out or None

    return RecurrenceRule(
        frequency=freq,
        interval=int(rule.interval()),
        end_date=end_date,
        end_count=end_count,
        days_of_week=days_of_week,
        days_of_month=_to_int_list(rule.daysOfTheMonth()),
        months_of_year=_to_int_list(rule.monthsOfTheYear()),
        set_positions=_to_int_list(rule.setPositions()),
    )


def _model_to_recurrence(model: RecurrenceRule) -> Any:
    freq = _FREQ_TO_EK[model.frequency]
    end = None
    if model.end_date is not None:
        end = EKRecurrenceEnd.recurrenceEndWithEndDate_(_datetime_to_ns_date(model.end_date))
    elif model.end_count is not None:
        end = EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(model.end_count)

    days_of_week = None
    if model.days_of_week:
        days_of_week = [
            EKRecurrenceDayOfWeek.dayOfWeek_(_DAY_TO_EK[d])
            for d in model.days_of_week
        ]

    return EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
        freq,
        max(1, int(model.interval)),
        days_of_week,
        model.days_of_month,
        model.months_of_year,
        None,
        None,
        model.set_positions,
        end,
    )


def _alarm_to_model(alarm: Any) -> AlarmSpec:
    abs_date = alarm.absoluteDate()
    if abs_date is not None:
        return AlarmSpec(kind="absolute", absolute_date=_ns_date_to_datetime(abs_date))
    return AlarmSpec(kind="relative", relative_offset_seconds=float(alarm.relativeOffset()))


def _model_to_alarm(spec: AlarmSpec) -> Any:
    if spec.kind == "absolute":
        if spec.absolute_date is None:
            raise ValueError("Absolute alarm requires absolute_date.")
        return EKAlarm.alarmWithAbsoluteDate_(_datetime_to_ns_date(spec.absolute_date))
    if spec.kind == "relative":
        return EKAlarm.alarmWithRelativeOffset_(float(spec.relative_offset_seconds or 0.0))
    raise ValueError(f"Unknown alarm kind: {spec.kind}")


def _structured_location_to_model(sloc: Any) -> Optional[LocationSpec]:
    if sloc is None:
        return None
    lat = lon = None
    geo = sloc.geoLocation()
    if geo is not None:
        coord = geo.coordinate()
        lat, lon = float(coord.latitude), float(coord.longitude)
    title = str(sloc.title()) if sloc.title() else ""
    if not title and lat is None:
        return None
    radius = None
    try:
        radius = float(sloc.radius()) if sloc.radius() > 0 else None
    except Exception:
        pass
    return LocationSpec(title=title, latitude=lat, longitude=lon, radius_meters=radius)


def _model_to_structured_location(spec: LocationSpec) -> Any:
    sl = EKStructuredLocation.locationWithTitle_(spec.title or "")
    if spec.latitude is not None and spec.longitude is not None:
        cl_loc = CLLocation.alloc().initWithLatitude_longitude_(spec.latitude, spec.longitude)
        sl.setGeoLocation_(cl_loc)
        if spec.radius_meters is not None:
            sl.setRadius_(float(spec.radius_meters))
    return sl


def _participant_to_model(p: Any, is_organizer: bool = False) -> Participant:
    email = None
    try:
        url = p.URL()
        if url is not None:
            s = str(url.absoluteString())
            if s.lower().startswith("mailto:"):
                email = s[7:]
    except Exception:
        pass
    try:
        status = _EK_TO_PSTATUS.get(int(p.participantStatus()), "unknown")
    except Exception:
        status = "unknown"
    try:
        role = _EK_TO_PROLE.get(int(p.participantRole()), "unknown")
    except Exception:
        role = "unknown"
    try:
        is_me = bool(p.isCurrentUser())
    except Exception:
        is_me = False
    return Participant(
        name=str(p.name()) if p.name() else None,
        email=email,
        status=status,  # type: ignore[arg-type]
        role=role,      # type: ignore[arg-type]
        is_current_user=is_me,
        is_organizer=is_organizer,
    )


def _make_event_link(event_id: str, occurrence: Optional[datetime]) -> str:
    """Build an ical:// URL that opens an event in Calendar.app (best effort).

    For occurrences of recurring events, Calendar.app disambiguates via a
    UTC timestamp path segment before the identifier.
    """
    uid = quote(event_id, safe="")
    if occurrence is not None:
        ts = occurrence.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"ical://ekevent/{ts}/{uid}"
    return f"ical://ekevent/{uid}"


def _nserror_str(err: Any) -> str:
    if err is None:
        return "unknown error"
    try:
        return str(err.localizedDescription())
    except Exception:
        return repr(err)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class CalendarStore:
    """Synchronous wrapper around EKEventStore for the Event entity."""

    def __init__(self) -> None:
        self._store = EKEventStore.alloc().init()
        self._access_granted = False
        self._calendars_by_id: dict[str, Any] = {}  # cache: id → EKCalendar
        self._events_by_key: dict[str, Any] = {}    # cache: id[@occurrence] → EKEvent

        self._ensure_access()
        self._refresh_calendars()

    # ------------------------------------------------------------------
    # Permission gate
    # ------------------------------------------------------------------

    def _ensure_access(self) -> None:
        if self._access_granted:
            return
        # Best-effort: check current authorization status before prompting.
        try:
            cls = EKEventStore.authorizationStatusForEntityType_
            status = int(cls(EKEntityTypeEvent))
            logger.info(
                "Calendar authorization status: %s (%d)",
                authorization_status_label(status), status,
            )
            if status in (3, 5):  # authorized / fullAccess
                self._access_granted = True
                return
            if status in (1, 2, 4):  # restricted / denied / writeOnly (cannot read)
                raise PermissionDeniedError(
                    f"Status: {authorization_status_label(status)}"
                )
        except AttributeError:
            pass

        granted = request_calendar_access(self._store)
        if not granted:
            raise PermissionDeniedError()
        self._access_granted = True

    # ------------------------------------------------------------------
    # Calendars
    # ------------------------------------------------------------------

    def _refresh_calendars(self) -> None:
        cals = self._store.calendarsForEntityType_(EKEntityTypeEvent) or []
        self._calendars_by_id = {str(c.calendarIdentifier()): c for c in cals}

    def _default_calendar_id(self) -> Optional[str]:
        default = self._store.defaultCalendarForNewEvents()
        return str(default.calendarIdentifier()) if default else None

    def _calendar_to_model(self, cal: Any, default_id: Optional[str] = None) -> CalendarInfo:
        if default_id is None:
            default_id = self._default_calendar_id()
        src = cal.source()
        try:
            is_subscribed = bool(cal.isSubscribed())
        except Exception:
            is_subscribed = False
        return CalendarInfo(
            id=str(cal.calendarIdentifier()),
            title=str(cal.title()),
            color=_color_to_hex(cal.CGColor()),
            source_name=str(src.title()) if src else "",
            source_type=_SOURCE_TYPE_LABEL.get(src.sourceType() if src else -1, "unknown"),
            allows_modification=bool(cal.allowsContentModifications()),
            is_default=(str(cal.calendarIdentifier()) == default_id),
            is_subscribed=is_subscribed,
        )

    def list_calendars(self) -> list[CalendarInfo]:
        self._refresh_calendars()
        default_id = self._default_calendar_id()
        return [
            self._calendar_to_model(c, default_id)
            for c in self._calendars_by_id.values()
        ]

    def _get_calendar(self, calendar_id: str) -> Any:
        cal = self._calendars_by_id.get(calendar_id)
        if cal is None:
            self._refresh_calendars()
            cal = self._calendars_by_id.get(calendar_id)
        if cal is None:
            raise ValueError(f"Calendar not found: {calendar_id}")
        return cal

    def create_calendar(
        self,
        title: str,
        color: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> CalendarResult:
        if not title or not title.strip():
            raise ValueError("Calendar title must be a non-empty string.")
        cal = EKCalendar.calendarForEntityType_eventStore_(EKEntityTypeEvent, self._store)
        cal.setTitle_(title.strip())

        # Choose a source: explicit by name, or first writable calendar source.
        chosen = None
        for src in self._store.sources():
            if source_name and source_name.lower() == str(src.title()).lower():
                chosen = src
                break
        if chosen is None:
            # Prefer iCloud / CalDAV; fall back to Local.
            preferred_order = (EKSourceTypeCalDAV, EKSourceTypeMobileMe, EKSourceTypeLocal)
            for st in preferred_order:
                for src in self._store.sources():
                    if src.sourceType() == st:
                        chosen = src
                        break
                if chosen is not None:
                    break
        if chosen is None:
            raise RuntimeError("No source available for new calendar.")
        cal.setSource_(chosen)

        if color:
            cgc = _hex_to_cgcolor(color)
            if cgc is not None:
                cal.setCGColor_(cgc)

        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not create calendar: {_nserror_str(err)}")

        self._refresh_calendars()
        return CalendarResult(calendar=self._calendar_to_model(cal), success=True)

    def update_calendar(
        self,
        calendar_id: str,
        title: Optional[str] = None,
        color: Optional[str] = None,
    ) -> CalendarResult:
        cal = self._get_calendar(calendar_id)
        if title is not None:
            if not title.strip():
                raise ValueError("Calendar title must be a non-empty string.")
            cal.setTitle_(title.strip())
        if color is not None:
            cgc = _hex_to_cgcolor(color)
            if cgc is None:
                raise ValueError(f"Invalid color: {color!r}. Use '#RRGGBB'.")
            cal.setCGColor_(cgc)
        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not update calendar: {_nserror_str(err)}")
        self._refresh_calendars()
        return CalendarResult(calendar=self._calendar_to_model(cal), success=True)

    def delete_calendar(self, calendar_id: str) -> CalendarResult:
        cal = self._get_calendar(calendar_id)
        # Count events over a wide window before deletion (purely informational).
        count: Optional[int] = None
        try:
            now = datetime.now().astimezone()
            rows = self._fetch_events(
                now - timedelta(days=2 * 365), now + timedelta(days=2 * 365), [cal]
            )
            count = len(rows)
        except Exception:
            pass
        ok, err = self._store.removeCalendar_commit_error_(cal, True, None)
        if not ok:
            raise RuntimeError(f"Could not delete calendar: {_nserror_str(err)}")
        self._refresh_calendars()
        return CalendarResult(calendar=None, success=True, deleted_event_count=count)

    # ------------------------------------------------------------------
    # Events — read
    # ------------------------------------------------------------------

    def _fetch_events(
        self,
        start: datetime,
        end: datetime,
        calendars: Optional[list[Any]],
    ) -> list[Any]:
        """Fetch event occurrences in [start, end], chunking past the 4-year
        predicate limit. Occurrences of recurring events appear individually."""
        if end <= start:
            return []
        rows: list[Any] = []
        seen: set[tuple[str, float]] = set()
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(end, chunk_start + timedelta(days=_MAX_PREDICATE_DAYS))
            pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                _datetime_to_ns_date(chunk_start),
                _datetime_to_ns_date(chunk_end),
                calendars,
            )
            for e in (self._store.eventsMatchingPredicate_(pred) or []):
                try:
                    key = (
                        str(e.eventIdentifier()),
                        e.startDate().timeIntervalSince1970() if e.startDate() else 0.0,
                    )
                except Exception:
                    key = (repr(e), 0.0)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(e)
            chunk_start = chunk_end
        return rows

    def _resolve_calendars(self, calendar_ids: Optional[list[str]]) -> Optional[list[Any]]:
        if not calendar_ids:
            return None
        return [self._get_calendar(cid) for cid in calendar_ids]

    def _cache_event(self, e: Any) -> None:
        try:
            ident = str(e.eventIdentifier())
        except Exception:
            return
        self._events_by_key[ident] = e
        occ = _ns_date_to_datetime(e.occurrenceDate()) if e.occurrenceDate() else None
        if occ is not None:
            self._events_by_key[f"{ident}@{occ.isoformat()}"] = e

    def _to_summary(self, e: Any) -> EventSummary:
        cal = e.calendar()
        ident = str(e.eventIdentifier()) if e.eventIdentifier() else str(e.calendarItemIdentifier())
        occurrence = _ns_date_to_datetime(e.occurrenceDate()) if e.occurrenceDate() else None
        has_recurrence = bool(e.hasRecurrenceRules())
        try:
            avail = _EK_TO_AVAIL.get(int(e.availability()), "busy")
        except Exception:
            avail = "busy"
        try:
            status = _EK_TO_STATUS.get(int(e.status()), "none")
        except Exception:
            status = "none"
        try:
            has_attendees = bool(e.hasAttendees())
        except Exception:
            has_attendees = bool(e.attendees())
        return EventSummary(
            id=ident,
            calendar_id=str(cal.calendarIdentifier()) if cal else "",
            calendar_title=str(cal.title()) if cal else "",
            title=str(e.title() or ""),
            start_date=_ns_date_to_datetime(e.startDate()),
            end_date=_ns_date_to_datetime(e.endDate()),
            all_day=bool(e.isAllDay()),
            location=str(e.location()) if e.location() else None,
            status=status,      # type: ignore[arg-type]
            availability=avail,  # type: ignore[arg-type]
            has_recurrence=has_recurrence,
            is_detached=bool(e.isDetached()),
            occurrence_date=occurrence if has_recurrence or bool(e.isDetached()) else None,
            has_attendees=has_attendees,
            event_link=_make_event_link(
                ident, occurrence if has_recurrence else None
            ),
        )

    def _to_detail(self, e: Any) -> EventDetail:
        summary = self._to_summary(e)
        notes = str(e.notes()) if e.notes() else None
        url_obj = e.URL()
        url_str = str(url_obj.absoluteString()) if url_obj else None

        tz = None
        try:
            ns_tz = e.timeZone()
            if ns_tz is not None:
                tz = str(ns_tz.name())
        except Exception:
            pass

        organizer = None
        try:
            org = e.organizer()
            if org is not None:
                organizer = _participant_to_model(org, is_organizer=True)
        except Exception:
            pass

        attendees = []
        for p in (e.attendees() or []):
            try:
                attendees.append(_participant_to_model(p))
            except Exception:
                continue

        rules = e.recurrenceRules() or []
        recurrence = _recurrence_to_model(rules[0]) if rules else None

        alarms = []
        for a in (e.alarms() or []):
            try:
                alarms.append(_alarm_to_model(a))
            except Exception:
                continue

        structured = None
        try:
            structured = _structured_location_to_model(e.structuredLocation())
        except Exception:
            pass

        return EventDetail(
            **summary.model_dump(),
            notes=notes,
            url=url_str,
            time_zone=tz,
            organizer=organizer,
            attendees=attendees,
            recurrence=recurrence,
            alarms=alarms,
            structured_location=structured,
            creation_date=_ns_date_to_datetime(e.creationDate()),
            modification_date=_ns_date_to_datetime(e.lastModifiedDate()),
        )

    def _post_filter(
        self,
        rows: Iterable[Any],
        text: Optional[str],
        all_day: Optional[bool],
        include_canceled: bool,
    ) -> list[Any]:
        text_lc = text.lower() if text else None
        out = []
        for e in rows:
            if not include_canceled:
                try:
                    if int(e.status()) == 3:  # canceled
                        continue
                except Exception:
                    pass
            if all_day is not None and bool(e.isAllDay()) != all_day:
                continue
            if text_lc is not None:
                title = (str(e.title() or "")).lower()
                notes = (str(e.notes() or "")).lower()
                loc = (str(e.location() or "")).lower()
                if text_lc not in title and text_lc not in notes and text_lc not in loc:
                    continue
            out.append(e)
        return out

    def list_events(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: Optional[list[str]] = None,
        text: Optional[str] = None,
        all_day: Optional[bool] = None,
        include_canceled: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[EventSummary]]:
        calendars = self._resolve_calendars(calendar_ids)
        rows = self._fetch_events(start, end, calendars)
        rows = self._post_filter(rows, text, all_day, include_canceled)

        def sort_key(e: Any) -> tuple[float, str]:
            d = e.startDate()
            ts = d.timeIntervalSince1970() if d else float("inf")
            return (ts, str(e.title() or ""))
        rows.sort(key=sort_key)

        total = len(rows)
        page = rows[offset : offset + limit]
        for e in page:
            self._cache_event(e)
        return total, [self._to_summary(e) for e in page]

    def get_event(
        self,
        event_id: str,
        occurrence_date: Optional[datetime] = None,
    ) -> Optional[EventDetail]:
        e = self._lookup_event(event_id, occurrence_date)
        if e is None:
            return None
        return self._to_detail(e)

    def get_event_link(
        self,
        event_id: str,
        occurrence_date: Optional[datetime] = None,
    ) -> str:
        e = self._lookup_event(event_id, occurrence_date)
        if e is None:
            raise ValueError(f"Event not found: {event_id}")
        occ = _ns_date_to_datetime(e.occurrenceDate()) if e.occurrenceDate() else None
        return _make_event_link(event_id, occ if bool(e.hasRecurrenceRules()) else None)

    def get_stats(self) -> CalendarStats:
        self._refresh_calendars()
        now = datetime.now().astimezone()
        today_start = _local_midnight(now)
        today_end = _local_end_of_day(now)
        week_end = today_start + timedelta(days=7)

        week_rows = self._fetch_events(today_start, week_end, None)
        week_rows = self._post_filter(week_rows, None, None, include_canceled=False)

        events_today = 0
        next_event: Optional[Any] = None
        next_start: Optional[float] = None
        for e in week_rows:
            s = _ns_date_to_datetime(e.startDate())
            if s is None:
                continue
            e_end = _ns_date_to_datetime(e.endDate()) or s
            if s <= today_end and e_end >= today_start:
                events_today += 1
            if s >= now and (next_start is None or s.timestamp() < next_start):
                next_start = s.timestamp()
                next_event = e
        return CalendarStats(
            calendar_count=len(self._calendars_by_id),
            events_today=events_today,
            events_next_7_days=len(week_rows),
            next_event=self._to_summary(next_event) if next_event is not None else None,
        )

    def get_availability(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: Optional[list[str]] = None,
        include_all_day: bool = False,
        include_tentative: bool = True,
    ) -> AvailabilityResult:
        """Merged busy intervals within [start, end].

        An event blocks time unless it is marked free, canceled, an all-day
        event (unless include_all_day), or declined by the current user.
        """
        calendars = self._resolve_calendars(calendar_ids)
        rows = self._fetch_events(start, end, calendars)

        intervals: list[tuple[datetime, datetime, str, str]] = []
        for e in rows:
            try:
                if int(e.status()) == 3:  # canceled
                    continue
            except Exception:
                pass
            if bool(e.isAllDay()) and not include_all_day:
                continue
            try:
                avail = int(e.availability())
            except Exception:
                avail = 0
            if avail == 1:  # free
                continue
            if avail == 2 and not include_tentative:  # tentative
                continue
            # Skip events the current user has declined.
            declined = False
            for p in (e.attendees() or []):
                try:
                    if bool(p.isCurrentUser()) and int(p.participantStatus()) == 3:
                        declined = True
                        break
                except Exception:
                    continue
            if declined:
                continue
            s = _ns_date_to_datetime(e.startDate())
            t = _ns_date_to_datetime(e.endDate())
            if s is None or t is None or t <= s:
                continue
            intervals.append((max(s, start), min(t, end),
                              str(e.eventIdentifier() or ""), str(e.title() or "")))

        intervals.sort(key=lambda x: x[0])
        merged: list[BusyInterval] = []
        for s, t, ident, title in intervals:
            if merged and s <= merged[-1].end:
                last = merged[-1]
                last.end = max(last.end, t)
                if ident and ident not in last.event_ids:
                    last.event_ids.append(ident)
                    last.titles.append(title)
            else:
                merged.append(BusyInterval(
                    start=s, end=t,
                    event_ids=[ident] if ident else [], titles=[title],
                ))
        return AvailabilityResult(start=start, end=end, busy=merged)

    # ------------------------------------------------------------------
    # Events — write
    # ------------------------------------------------------------------

    def _lookup_event(
        self,
        event_id: str,
        occurrence_date: Optional[datetime] = None,
    ) -> Optional[Any]:
        if occurrence_date is not None:
            cached = self._events_by_key.get(f"{event_id}@{occurrence_date.isoformat()}")
            if cached is not None:
                return cached
            # Fetch a window around the occurrence and pick the matching one.
            window_start = occurrence_date - timedelta(days=2)
            window_end = occurrence_date + timedelta(days=2)
            candidates = [
                e for e in self._fetch_events(window_start, window_end, None)
                if str(e.eventIdentifier()) == event_id
            ]
            best = None
            best_delta = None
            for e in candidates:
                for anchor in (e.occurrenceDate(), e.startDate()):
                    d = _ns_date_to_datetime(anchor)
                    if d is None:
                        continue
                    delta = abs((d - occurrence_date).total_seconds())
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        best = e
            if best is not None:
                self._cache_event(best)
                return best
            # Fall through to the identifier lookup (master event).

        cached = self._events_by_key.get(event_id)
        if cached is not None:
            return cached
        e = self._store.eventWithIdentifier_(event_id)
        if e is None:
            # eventIdentifier and calendarItemIdentifier differ; accept either.
            e = self._store.calendarItemWithIdentifier_(event_id)
        if e is None:
            return None
        self._cache_event(e)
        return e

    def create_event(
        self,
        title: str,
        start_date: datetime,
        end_date: Optional[datetime] = None,
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
    ) -> EventDetail:
        if not title or not title.strip():
            raise ValueError("Event title must be a non-empty string.")

        e = EKEvent.eventWithEventStore_(self._store)
        e.setTitle_(title.strip())

        cal: Any
        if calendar_id:
            cal = self._get_calendar(calendar_id)
        else:
            cal = self._store.defaultCalendarForNewEvents()
            if cal is None:
                raise RuntimeError("No default calendar available for new events.")
        e.setCalendar_(cal)

        if all_day:
            e.setAllDay_(True)
            norm_start = _local_midnight(start_date)
            norm_end = _local_end_of_day(end_date if end_date is not None else start_date)
            if norm_end < norm_start:
                raise ValueError("end_date must not be before start_date.")
            e.setStartDate_(_datetime_to_ns_date(norm_start))
            e.setEndDate_(_datetime_to_ns_date(norm_end))
        else:
            eff_end = end_date if end_date is not None else start_date + timedelta(hours=1)
            if eff_end <= start_date:
                raise ValueError("end_date must be after start_date.")
            e.setStartDate_(_datetime_to_ns_date(start_date))
            e.setEndDate_(_datetime_to_ns_date(eff_end))

        if time_zone is not None:
            ns_tz = NSTimeZone.timeZoneWithName_(time_zone)
            if ns_tz is None:
                raise ValueError(f"Unknown time zone: {time_zone!r}. Use an IANA name.")
            e.setTimeZone_(ns_tz)

        if notes is not None:
            e.setNotes_(notes)
        if url is not None:
            ns_url = NSURL.URLWithString_(url)
            if ns_url is not None:
                e.setURL_(ns_url)
        if location is not None:
            e.setLocation_(location)
        if structured_location is not None:
            e.setStructuredLocation_(_model_to_structured_location(structured_location))
        if availability is not None:
            e.setAvailability_(_AVAIL_TO_EK[availability])

        if recurrence is not None:
            e.setRecurrenceRules_([_model_to_recurrence(recurrence)])

        if alarms:
            for spec in alarms:
                e.addAlarm_(_model_to_alarm(spec))

        ok, err = self._store.saveEvent_span_commit_error_(e, EK_SPAN_THIS_EVENT, True, None)
        if not ok:
            raise RuntimeError(f"Could not save event: {_nserror_str(err)}")

        self._cache_event(e)
        return self._to_detail(e)

    def update_event(
        self,
        event_id: str,
        occurrence_date: Optional[datetime] = None,
        span: Span = "this_event",
        title: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
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
    ) -> EventDetail:
        e = self._lookup_event(event_id, occurrence_date)
        if e is None:
            raise ValueError(f"Event not found: {event_id}")

        if title is not None:
            if not title.strip():
                raise ValueError("Event title must be a non-empty string.")
            e.setTitle_(title.strip())

        if all_day is not None:
            e.setAllDay_(bool(all_day))

        if start_date is not None or end_date is not None:
            cur_start = _ns_date_to_datetime(e.startDate())
            cur_end = _ns_date_to_datetime(e.endDate())
            new_start = start_date if start_date is not None else cur_start
            new_end = end_date if end_date is not None else cur_end
            if new_start is None:
                raise ValueError("Event has no start date; provide start_date.")
            if bool(e.isAllDay()):
                new_start = _local_midnight(new_start)
                new_end = _local_end_of_day(new_end if new_end is not None else new_start)
            elif new_end is None or new_end <= new_start:
                if end_date is not None:
                    raise ValueError("end_date must be after start_date.")
                # Start moved past the old end: preserve the original duration.
                duration = (cur_end - cur_start) if (cur_end and cur_start) else timedelta(hours=1)
                new_end = new_start + duration
            e.setStartDate_(_datetime_to_ns_date(new_start))
            e.setEndDate_(_datetime_to_ns_date(new_end))

        if time_zone is not None:
            ns_tz = NSTimeZone.timeZoneWithName_(time_zone)
            if ns_tz is None:
                raise ValueError(f"Unknown time zone: {time_zone!r}. Use an IANA name.")
            e.setTimeZone_(ns_tz)

        if calendar_id is not None:
            e.setCalendar_(self._get_calendar(calendar_id))

        if clear_notes:
            e.setNotes_(None)
        elif notes is not None:
            e.setNotes_(notes)

        if clear_url:
            e.setURL_(None)
        elif url is not None:
            ns_url = NSURL.URLWithString_(url)
            if ns_url is not None:
                e.setURL_(ns_url)

        if clear_location:
            e.setLocation_(None)
            e.setStructuredLocation_(None)
        else:
            if location is not None:
                e.setLocation_(location)
            if structured_location is not None:
                e.setStructuredLocation_(_model_to_structured_location(structured_location))

        if availability is not None:
            e.setAvailability_(_AVAIL_TO_EK[availability])

        if clear_recurrence:
            e.setRecurrenceRules_(None)
        elif recurrence is not None:
            e.setRecurrenceRules_([_model_to_recurrence(recurrence)])

        if clear_alarms:
            for a in list(e.alarms() or []):
                e.removeAlarm_(a)
        elif alarms is not None:
            for a in list(e.alarms() or []):
                e.removeAlarm_(a)
            for spec in alarms:
                e.addAlarm_(_model_to_alarm(spec))

        ok, err = self._store.saveEvent_span_commit_error_(
            e, _SPAN_TO_EK[span], True, None
        )
        if not ok:
            raise RuntimeError(f"Could not update event: {_nserror_str(err)}")
        return self._to_detail(e)

    def delete_event(
        self,
        event_id: str,
        occurrence_date: Optional[datetime] = None,
        span: Span = "this_event",
    ) -> DeleteResult:
        e = self._lookup_event(event_id, occurrence_date)
        if e is None:
            raise ValueError(f"Event not found: {event_id}")
        ok, err = self._store.removeEvent_span_commit_error_(
            e, _SPAN_TO_EK[span], True, None
        )
        if not ok:
            raise RuntimeError(f"Could not delete event: {_nserror_str(err)}")
        self._events_by_key.pop(event_id, None)
        if occurrence_date is not None:
            self._events_by_key.pop(f"{event_id}@{occurrence_date.isoformat()}", None)
        return DeleteResult(id=event_id, success=True, span=span)
