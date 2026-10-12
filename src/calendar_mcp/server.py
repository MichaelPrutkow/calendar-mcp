 # type: ignore
import threading
from datetime import datetime

import EventKit
from Foundation import NSDate
from mcp.server import MCPServer

mcp = MCPServer("apple-calendar")
store = EventKit.EKEventStore.alloc().init()
_access = False

FREQUENCIES = {
    "daily": EventKit.EKRecurrenceFrequencyDaily,
    "weekly": EventKit.EKRecurrenceFrequencyWeekly,
    "monthly": EventKit.EKRecurrenceFrequencyMonthly,
    "yearly": EventKit.EKRecurrenceFrequencyYearly,
}
WEEKDAYS = {
    "mon": EventKit.EKMonday,
    "tue": EventKit.EKTuesday,
    "wed": EventKit.EKWednesday,
    "thu": EventKit.EKThursday,
    "fri": EventKit.EKFriday,
    "sat": EventKit.EKSaturday,
    "sun": EventKit.EKSunday,
}


def ensure_access():
    global _access
    if _access:
        return
    done, result = threading.Event(), {}

    def cb(granted, error):
        result["granted"] = granted
        result["error"] = error
        done.set()

    store.requestFullAccessToEventsWithCompletion_(cb)
    done.wait(60)
    if not result.get("granted"):
        raise PermissionError(f"Access denied: {result.get('error')}")
    _access = True


def to_nsdate(iso: str) -> NSDate:
    # naive strings (no offset) are interpreted as local time
    return NSDate.dateWithTimeIntervalSince1970_(datetime.fromisoformat(iso).timestamp())


def from_nsdate(ns: NSDate) -> str:
    return datetime.fromtimestamp(ns.timeIntervalSince1970()).astimezone().isoformat()


def all_calendars():
    return list(store.calendarsForEntityType_(EventKit.EKEntityTypeEvent))


def find_calendar(name: str):
    """Look up a calendar by its title (case-insensitive); EventKit needs the object, not the name."""
    calendars = all_calendars()
    for c in calendars:
        if c.title().lower() == name.lower():
            return c
    available = ", ".join(c.title() for c in calendars)
    raise ValueError(f"Calendar '{name}' not found. Available: {available}")


@mcp.tool()
def list_calendars() -> list[dict]:
    """List all calendars that can hold events, with their account and whether they are writable."""
    ensure_access()
    default = store.defaultCalendarForNewEvents()
    return [
        {
            "title": c.title(),
            "account": c.source().title(),
            "writable": bool(c.allowsContentModifications()),
            "is_default": default is not None and c.calendarIdentifier() == default.calendarIdentifier(),
        }
        for c in all_calendars()
    ]


@mcp.tool()
def list_events(days: int = 7, calendar: str | None = None) -> list[dict]:
    """List events from now until `days` days ahead, optionally only from the calendar with this title."""
    ensure_access()
    start = NSDate.date()
    end = NSDate.dateWithTimeIntervalSinceNow_(days * 86400)
    calendars = [find_calendar(calendar)] if calendar else None
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, calendars)
    return [
        {
            "title": e.title(),
            "start": from_nsdate(e.startDate()),
            "end": from_nsdate(e.endDate()),
            "all_day": bool(e.isAllDay()),
            "calendar": e.calendar().title(),
            "location": e.location(),
            "notes": e.notes(),
            "recurring": bool(e.hasRecurrenceRules()),
        }
        for e in store.eventsMatchingPredicate_(pred)
    ]


@mcp.tool()
def create_event(
    title: str,
    start: str,
    end: str,
    calendar: str | None = None,
    location: str | None = None,
    notes: str = "",
    all_day: bool = False,
    repeat: str | None = None,
    repeat_interval: int = 1,
    repeat_days: list[str] | None = None,
    repeat_until: str | None = None,
    repeat_count: int | None = None,
) -> str:
    """Create a calendar event, optionally recurring.

    start/end: ISO-8601, e.g. 2026-10-17T16:30:00 (no offset = local time).
    calendar: calendar title as returned by list_calendars; default calendar if omitted.
    repeat: one of daily, weekly, monthly, yearly (omit for a single event).
    repeat_interval: every N days/weeks/months/years (default 1).
    repeat_days: for weekly repeats, a list of mon, tue, wed, thu, fri, sat, sun.
    repeat_until: ISO-8601 date/time when the series ends (inclusive), OR
    repeat_count: number of occurrences. Without either, the series never ends.
    """
    ensure_access()
    ev = EventKit.EKEvent.eventWithEventStore_(store)
    ev.setTitle_(title)
    ev.setStartDate_(to_nsdate(start))
    ev.setEndDate_(to_nsdate(end))
    ev.setAllDay_(all_day)
    ev.setNotes_(notes)
    if location:
        ev.setLocation_(location)
    ev.setCalendar_(find_calendar(calendar) if calendar else store.defaultCalendarForNewEvents())

    if repeat:
        if repeat.lower() not in FREQUENCIES:
            raise ValueError(f"repeat must be one of {', '.join(FREQUENCIES)}")
        if repeat_until and repeat_count:
            raise ValueError("Use either repeat_until or repeat_count, not both")
        series_end = None
        if repeat_until:
            series_end = EventKit.EKRecurrenceEnd.recurrenceEndWithEndDate_(to_nsdate(repeat_until))
        elif repeat_count:
            series_end = EventKit.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(repeat_count)
        days = None
        if repeat_days:
            days = [EventKit.EKRecurrenceDayOfWeek.dayOfWeek_(WEEKDAYS[d.lower()[:3]]) for d in repeat_days]
        rule = EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
            FREQUENCIES[repeat.lower()], repeat_interval, days, None, None, None, None, None, series_end
        )
        ev.addRecurrenceRule_(rule)

    ok, err = store.saveEvent_span_error_(ev, EventKit.EKSpanFutureEvents, None)
    if not ok:
        raise RuntimeError(str(err))
    return f"Event '{title}' created in '{ev.calendar().title()}'."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
