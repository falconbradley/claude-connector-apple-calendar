"""
End-to-end tests for the Apple Calendar MCP connector.

Tests are split into two groups:

  Group A — Static tests (no Calendar permission required)
    Pydantic model shapes, input validation, pure helpers
    (interval merging, link building, date normalisation).
    Always run.

  Group B — Live EventKit tests (requires Calendar access)
    Operate against a dedicated test calendar named ``__claude_mcp_test__``,
    which is created at setup and torn down at the end. Skipped with a
    clear message if Calendar access has not been granted.

Usage:
    uv run python tests/test_e2e.py               # full suite
    uv run python tests/test_e2e.py --skip-live   # Group A only
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

# Ensure src/ is on sys.path so the package imports cleanly when run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TEST_CAL = "__claude_mcp_test__"


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"

_registry: list[tuple[str, str, Callable]] = []   # (group, name, fn)
_results: list[tuple[str, str, str, str]] = []    # (status, group, name, detail)


def test(group: str, name: str):
    def decorator(fn):
        _registry.append((group, name, fn))
        return fn
    return decorator


class _SkipTest(Exception):
    """Raised by a test when a required fixture is unavailable."""


def skip(msg: str):
    raise _SkipTest(msg)


def run_all(skip_live: bool = False) -> None:
    for group, name, fn in _registry:
        if skip_live and group == "B":
            _results.append((_SKIP, group, name, "live tests skipped"))
            continue
        try:
            fn()
            _results.append((_PASS, group, name, ""))
        except _SkipTest as exc:
            _results.append((_SKIP, group, name, str(exc)))
        except AssertionError as exc:
            _results.append((_FAIL, group, name, str(exc)))
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            _results.append((_FAIL, group, name, f"{type(exc).__name__}: {exc}\n{tb}"))


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"Expected {b!r}, got {a!r}" + (f" — {msg}" if msg else ""))


def is_in(v, c, msg=""):
    if v not in c:
        raise AssertionError(f"{v!r} not in {c!r}" + (f" — {msg}" if msg else ""))


def truthy(v, msg=""):
    if not v:
        raise AssertionError(f"Expected truthy, got {v!r}" + (f" — {msg}" if msg else ""))


def not_none(v, msg=""):
    if v is None:
        raise AssertionError("Expected non-None" + (f" — {msg}" if msg else ""))


# ---------------------------------------------------------------------------
# Group A — Static (models + validation + pure helpers)
# ---------------------------------------------------------------------------

@test("A", "model shapes")
def t_model_shapes():
    from apple_calendar_mcp.models import (
        AlarmSpec,
        AvailabilityResult,
        BusyInterval,
        CalendarInfo,
        CalendarResult,
        CalendarStats,
        DeleteResult,
        EventDetail,
        EventResult,
        EventSummary,
        LocationSpec,
        RecurrenceRule,
        SearchResult,
    )

    ci = CalendarInfo(id="abc", title="Work", source_name="iCloud", source_type="calDAV")
    eq(ci.allows_modification, True)
    eq(ci.is_default, False)

    es = EventSummary(id="e1", calendar_id="abc", calendar_title="Work", title="Standup")
    eq(es.all_day, False)
    eq(es.availability, "busy")
    eq(es.status, "none")

    now = datetime.now(timezone.utc)
    ed = EventDetail(
        id="e1", calendar_id="abc", calendar_title="Work", title="Standup",
        start_date=now, end_date=now + timedelta(minutes=30),
    )
    eq(ed.attendees, [])
    eq(ed.alarms, [])

    rr = RecurrenceRule(frequency="weekly", days_of_week=["monday", "wednesday"])
    eq(rr.interval, 1)

    al = AlarmSpec(kind="relative", relative_offset_seconds=-600)
    eq(al.absolute_date, None)

    ls = LocationSpec(title="HQ", latitude=37.77, longitude=-122.42)
    eq(ls.radius_meters, None)

    sr = SearchResult(total=0, offset=0, limit=50, events=[])
    eq(sr.events, [])

    st = CalendarStats(calendar_count=1, events_today=0, events_next_7_days=0)
    eq(st.next_event, None)

    bi = BusyInterval(start=now, end=now + timedelta(hours=1))
    ar = AvailabilityResult(start=now, end=now + timedelta(hours=8), busy=[bi])
    eq(len(ar.busy), 1)

    cr = CalendarResult(calendar=ci, success=True)
    truthy(cr.success)
    er = EventResult(event=ed, success=True)
    truthy(er.success)
    dr = DeleteResult(id="e1", success=True)
    eq(dr.span, "this_event")


@test("A", "recurrence validation rejects bad values")
def t_recurrence_validation():
    from pydantic import ValidationError
    from apple_calendar_mcp.models import RecurrenceRule

    try:
        RecurrenceRule(frequency="hourly")
        raise AssertionError("frequency='hourly' should be rejected")
    except ValidationError:
        pass
    try:
        RecurrenceRule(frequency="daily", interval=0)
        raise AssertionError("interval=0 should be rejected")
    except ValidationError:
        pass


@test("A", "ISO parsing in server helper")
def t_iso_parsing():
    from apple_calendar_mcp.server import _parse_iso

    eq(_parse_iso(None, "x"), None)
    d = _parse_iso("2026-08-24T10:00:00Z", "x")
    not_none(d)
    eq(d.tzinfo is not None, True)
    d2 = _parse_iso("2026-08-24T10:00:00", "x")  # naive → local
    eq(d2.tzinfo is not None, True)
    try:
        _parse_iso("not-a-date", "x")
        raise AssertionError("invalid date should raise")
    except ValueError:
        pass


@test("A", "event link building")
def t_event_link():
    from apple_calendar_mcp.calendars import _make_event_link

    eq(_make_event_link("ABC-123", None), "ical://ekevent/ABC-123")
    occ = datetime(2026, 8, 24, 17, 0, 0, tzinfo=timezone.utc)
    eq(_make_event_link("ABC/123", occ), "ical://ekevent/20260824T170000Z/ABC%2F123")


@test("A", "all-day date normalisation helpers")
def t_all_day_normalisation():
    from apple_calendar_mcp.calendars import _local_end_of_day, _local_midnight

    d = datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc)
    m = _local_midnight(d)
    eq((m.hour, m.minute, m.second), (0, 0, 0))
    e = _local_end_of_day(d)
    eq((e.hour, e.minute, e.second), (23, 59, 59))


@test("A", "server tool registry matches manifest")
def t_manifest_tools():
    import json
    import re

    manifest = {t["name"] for t in json.load(open(ROOT / "manifest.json"))["tools"]}
    src = open(ROOT / "src" / "apple_calendar_mcp" / "server.py").read()
    code = set(re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)", src))
    eq(code, manifest, "manifest.json tools must match @mcp.tool() defs in server.py")


@test("A", "version consistency")
def t_versions():
    import json
    import re

    from apple_calendar_mcp import __version__

    manifest = json.load(open(ROOT / "manifest.json"))["version"]
    pyproject = re.search(
        r'^version = "(.*)"', open(ROOT / "pyproject.toml").read(), re.M
    ).group(1)
    eq(manifest, pyproject, "manifest.json vs pyproject.toml")
    eq(__version__, pyproject, "__init__.__version__ vs pyproject.toml")


# ---------------------------------------------------------------------------
# Group B — Live EventKit tests
# ---------------------------------------------------------------------------

_live_store = None
_live_error: Optional[str] = None
_test_calendar_id: Optional[str] = None


def _get_live_store():
    """Initialise the CalendarStore once; skip live tests if access is denied."""
    global _live_store, _live_error
    if _live_store is not None:
        return _live_store
    if _live_error is not None:
        skip(_live_error)
    try:
        from apple_calendar_mcp.calendars import CalendarStore
        _live_store = CalendarStore()
        return _live_store
    except Exception as exc:
        _live_error = f"Calendar access unavailable: {type(exc).__name__}: {exc}"
        skip(_live_error)


def _get_test_calendar_id() -> str:
    global _test_calendar_id
    store = _get_live_store()
    if _test_calendar_id is not None:
        return _test_calendar_id
    # Reuse a leftover test calendar if a previous run crashed mid-way.
    for c in store.list_calendars():
        if c.title == TEST_CAL:
            _test_calendar_id = c.id
            return _test_calendar_id
    res = store.create_calendar(title=TEST_CAL, color="#FF8800")
    _test_calendar_id = res.calendar.id
    return _test_calendar_id


@test("B", "list calendars")
def t_live_list_calendars():
    store = _get_live_store()
    cals = store.list_calendars()
    truthy(len(cals) >= 1, "expected at least one calendar")
    defaults = [c for c in cals if c.is_default]
    truthy(len(defaults) <= 1, "at most one default calendar")


@test("B", "create test calendar")
def t_live_create_calendar():
    cal_id = _get_test_calendar_id()
    store = _get_live_store()
    cals = {c.id: c for c in store.list_calendars()}
    is_in(cal_id, cals)
    eq(cals[cal_id].title, TEST_CAL)


@test("B", "create + get + link for a timed event")
def t_live_create_event():
    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    start = datetime.now().astimezone() + timedelta(days=1)
    detail = store.create_event(
        title="MCP test event",
        start_date=start,
        end_date=start + timedelta(minutes=45),
        calendar_id=cal_id,
        notes="created by test_e2e",
        location="Test HQ",
        availability="busy",
    )
    eq(detail.title, "MCP test event")
    eq(detail.calendar_id, cal_id)
    eq(detail.all_day, False)
    eq(detail.location, "Test HQ")
    truthy(detail.id)

    got = store.get_event(detail.id)
    not_none(got)
    eq(got.notes, "created by test_e2e")

    link = store.get_event_link(detail.id)
    truthy(link.startswith("ical://ekevent/"), link)


@test("B", "list + search find the event")
def t_live_list_search():
    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    now = datetime.now().astimezone()
    total, rows = store.list_events(
        start=now, end=now + timedelta(days=3), calendar_ids=[cal_id],
        limit=50, offset=0,
    )
    truthy(total >= 1, "expected the created event in range")
    total2, rows2 = store.list_events(
        start=now, end=now + timedelta(days=3), calendar_ids=[cal_id],
        text="mcp test", limit=50, offset=0,
    )
    truthy(total2 >= 1, "text filter should match the created event")


@test("B", "all-day event round-trip")
def t_live_all_day():
    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    day = datetime.now().astimezone() + timedelta(days=2)
    detail = store.create_event(
        title="MCP all-day test", start_date=day, all_day=True, calendar_id=cal_id,
    )
    eq(detail.all_day, True)
    s = detail.start_date.astimezone()
    eq((s.hour, s.minute), (0, 0), "all-day start should be local midnight")


@test("B", "update event (times, notes, clear flags)")
def t_live_update():
    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    now = datetime.now().astimezone()
    total, rows = store.list_events(
        start=now, end=now + timedelta(days=3), calendar_ids=[cal_id],
        text="MCP test event", limit=10, offset=0,
    )
    truthy(total >= 1, "test event must exist")
    ev = rows[0]

    new_start = now + timedelta(days=1, hours=2)
    updated = store.update_event(
        event_id=ev.id,
        title="MCP test event (moved)",
        start_date=new_start,
        notes="updated",
    )
    eq(updated.title, "MCP test event (moved)")
    eq(updated.notes, "updated")
    # Duration was preserved when only the start moved.
    dur = (updated.end_date - updated.start_date).total_seconds()
    eq(int(dur), 45 * 60, "duration should be preserved")

    cleared = store.update_event(event_id=ev.id, clear_notes=True)
    eq(cleared.notes, None)


@test("B", "recurring event: occurrences + future_events delete")
def t_live_recurrence():
    from apple_calendar_mcp.models import RecurrenceRule

    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    start = datetime.now().astimezone() + timedelta(days=7)
    detail = store.create_event(
        title="MCP recurring test",
        start_date=start,
        end_date=start + timedelta(minutes=30),
        calendar_id=cal_id,
        recurrence=RecurrenceRule(frequency="daily", end_count=5),
    )
    truthy(detail.has_recurrence)
    not_none(detail.recurrence)
    eq(detail.recurrence.frequency, "daily")
    eq(detail.recurrence.end_count, 5)

    total, rows = store.list_events(
        start=start - timedelta(hours=1),
        end=start + timedelta(days=6),
        calendar_ids=[cal_id],
        text="MCP recurring test",
        limit=50, offset=0,
    )
    eq(total, 5, "expected 5 occurrences")

    # Delete occurrence 3 onward.
    third = rows[2]
    store.delete_event(third.id, occurrence_date=third.occurrence_date, span="future_events")
    total2, _ = store.list_events(
        start=start - timedelta(hours=1),
        end=start + timedelta(days=6),
        calendar_ids=[cal_id],
        text="MCP recurring test",
        limit=50, offset=0,
    )
    eq(total2, 2, "future_events delete should leave 2 occurrences")


@test("B", "availability merges busy intervals")
def t_live_availability():
    store = _get_live_store()
    cal_id = _get_test_calendar_id()
    base = datetime.now().astimezone() + timedelta(days=14)
    a = store.create_event(
        title="MCP busy A", start_date=base, end_date=base + timedelta(hours=1),
        calendar_id=cal_id,
    )
    b = store.create_event(
        title="MCP busy B", start_date=base + timedelta(minutes=30),
        end_date=base + timedelta(hours=2), calendar_id=cal_id,
    )
    res = store.get_availability(
        start=base - timedelta(hours=1), end=base + timedelta(hours=3),
        calendar_ids=[cal_id],
    )
    eq(len(res.busy), 1, "overlapping events should merge into one interval")
    eq(res.busy[0].start, base)
    eq(res.busy[0].end, base + timedelta(hours=2))
    store.delete_event(a.id)
    store.delete_event(b.id)


@test("B", "get_stats returns sane counts")
def t_live_stats():
    store = _get_live_store()
    stats = store.get_stats()
    truthy(stats.calendar_count >= 1)
    truthy(stats.events_next_7_days >= 0)


@test("B", "teardown: delete test calendar")
def t_live_teardown():
    global _test_calendar_id
    store = _get_live_store()
    if _test_calendar_id is None:
        skip("no test calendar was created")
    res = store.delete_calendar(_test_calendar_id)
    truthy(res.success)
    _test_calendar_id = None
    for c in store.list_calendars():
        if c.title == TEST_CAL:
            raise AssertionError("test calendar still present after deletion")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    skip_live = "--skip-live" in sys.argv
    run_all(skip_live=skip_live)

    width = max(len(name) for _, name, _ in _registry) + 2
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for status, group, name, detail in _results:
        counts[status] += 1
        line = f"  [{status}] ({group}) {name:<{width}}"
        if detail and status != "PASS":
            first = detail.splitlines()[0]
            line += f" {first}"
        print(line)
        if status == "FAIL" and "\n" in detail:
            for ln in detail.splitlines()[1:]:
                print(f"          {ln}")

    print()
    print(f"  {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
