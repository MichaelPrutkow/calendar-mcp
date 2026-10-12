# type: ignore
import threading
from datetime import datetime, timedelta
from typing import Annotated, Literal, TypedDict

import EventKit
from Foundation import NSDate, NSDateComponents, NSDateComponentUndefined
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = MCPServer(
    "apple-calendar",
    instructions=(
        "Access to the user's Apple Calendar events and Reminders on this Mac. "
        "Dates are ISO-8601 in the user's local time zone unless an offset is given. "
        "Calendar and list names must match a title from list_calendars or list_reminder_lists."
    ),
)
store = EventKit.EKEventStore.alloc().init()

EVENTS = EventKit.EKEntityTypeEvent
REMINDERS = EventKit.EKEntityTypeReminder

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

Frequency = Literal["daily", "weekly", "monthly", "yearly"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Priority = Literal["none", "high", "medium", "low"]

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
PRIORITIES = {"none": 0, "high": 1, "medium": 5, "low": 9}

Notes = Annotated[str | None, Field(description="Free text notes.")]
Repeat = Annotated[Frequency | None, Field(description="Repeat frequency. Omit for a one-off.")]
RepeatInterval = Annotated[int, Field(ge=1, description="Repeat every n days/weeks/months/years.")]
RepeatDays = Annotated[list[Weekday] | None, Field(description="Weekdays for weekly repeats.")]
RepeatUntil = Annotated[str | None, Field(description="Last date of the series. Use this or repeat_count.")]
RepeatCount = Annotated[int | None, Field(ge=1, description="Number of occurrences. Use this or repeat_until.")]


class Calendar(TypedDict):
    title: str
    account: str
    writable: bool
    default: bool


class Event(TypedDict):
    title: str
    start: str
    end: str
    all_day: bool
    calendar: str
    location: str | None
    notes: str | None
    recurring: bool


class Reminder(TypedDict):
    title: str
    due: str | None
    list: str
    notes: str | None
    priority: Priority
    recurring: bool


_authorized = set()


def wait_for(call, *args):
    done = threading.Event()
    result = []

    def callback(*values):
        result.extend(values)
        done.set()

    call(*args, callback)
    if not done.wait(60):
        raise ToolError("No response from EventKit.")
    return result


def ensure_access(entity_type):
    if entity_type in _authorized:
        return
    if entity_type == EVENTS:
        request = store.requestFullAccessToEventsWithCompletion_
    else:
        request = store.requestFullAccessToRemindersWithCompletion_
    granted, error = wait_for(request)
    if not granted:
        raise ToolError("Access denied. Allow it in System Settings > Privacy & Security.")
    _authorized.add(entity_type)


def parse_date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Invalid date '{value}'. Use ISO-8601, e.g. 2026-10-17 or 2026-10-17T16:30.") from None


def to_nsdate(value: datetime) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


def to_iso(date: NSDate, date_only: bool = False) -> str:
    value = datetime.fromtimestamp(date.timeIntervalSince1970()).astimezone()
    return value.date().isoformat() if date_only else value.isoformat(timespec="minutes")


def to_components(value: str) -> NSDateComponents:
    date = parse_date(value)
    if date.tzinfo:
        date = date.astimezone()
    components = NSDateComponents.alloc().init()
    components.setYear_(date.year)
    components.setMonth_(date.month)
    components.setDay_(date.day)
    if len(value) > 10:
        components.setHour_(date.hour)
        components.setMinute_(date.minute)
    return components


def from_components(components) -> str | None:
    if components is None:
        return None
    date = f"{components.year():04d}-{components.month():02d}-{components.day():02d}"
    if components.hour() == NSDateComponentUndefined:
        return date
    return f"{date}T{components.hour():02d}:{components.minute():02d}"


def priority_name(value: int) -> Priority:
    if value == 0:
        return "none"
    if value < 5:
        return "high"
    return "medium" if value == 5 else "low"


def find_calendar(name: str, entity_type):
    calendars = store.calendarsForEntityType_(entity_type)
    for calendar in calendars:
        if calendar.title().casefold() == name.casefold():
            return calendar
    raise ToolError(f"'{name}' not found. Available: {', '.join(c.title() for c in calendars)}")


def describe_calendars(entity_type, default) -> list[Calendar]:
    default_id = default.calendarIdentifier() if default else None
    return [
        {
            "title": c.title(),
            "account": c.source().title(),
            "writable": bool(c.allowsContentModifications()),
            "default": c.calendarIdentifier() == default_id,
        }
        for c in store.calendarsForEntityType_(entity_type)
    ]


def recurrence_rule(repeat, interval, days, until, count):
    if until and count:
        raise ToolError("Use either repeat_until or repeat_count, not both.")
    end = None
    if until:
        last = parse_date(until)
        if len(until) == 10:
            last += timedelta(days=1, seconds=-1)
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithEndDate_(to_nsdate(last))
    elif count:
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(count)
    weekdays = [EventKit.EKRecurrenceDayOfWeek.dayOfWeek_(WEEKDAYS[d]) for d in days] if days else None
    return EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
        FREQUENCIES[repeat], interval, weekdays, None, None, None, None, None, end
    )


@mcp.tool(title="List calendars", annotations=READ_ONLY)
def list_calendars() -> list[Calendar]:
    """List all event calendars."""
    ensure_access(EVENTS)
    return describe_calendars(EVENTS, store.defaultCalendarForNewEvents())


@mcp.tool(title="List events", annotations=READ_ONLY)
def list_events(
    start: Annotated[str | None, Field(description="Start of the range. Defaults to now.")] = None,
    days: Annotated[int, Field(ge=1, le=1460, description="Length of the range in days.")] = 7,
    calendar: Annotated[str | None, Field(description="Only events from this calendar.")] = None,
) -> list[Event]:
    """List events in a time range, sorted by start."""
    ensure_access(EVENTS)
    begin = parse_date(start) if start else datetime.now()
    calendars = [find_calendar(calendar, EVENTS)] if calendar else None
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        to_nsdate(begin), to_nsdate(begin + timedelta(days=days)), calendars
    )
    events = sorted(store.eventsMatchingPredicate_(predicate), key=lambda e: e.startDate().timeIntervalSince1970())
    return [
        {
            "title": e.title() or "",
            "start": to_iso(e.startDate(), e.isAllDay()),
            "end": to_iso(e.endDate(), e.isAllDay()),
            "all_day": bool(e.isAllDay()),
            "calendar": e.calendar().title(),
            "location": e.location(),
            "notes": e.notes(),
            "recurring": bool(e.hasRecurrenceRules()),
        }
        for e in events
    ]


@mcp.tool(title="Create event", annotations=CREATE)
def create_event(
    title: str,
    start: Annotated[str, Field(description="Start date or datetime, e.g. 2026-10-17T16:30.")],
    end: Annotated[str, Field(description="End date or datetime.")],
    calendar: Annotated[str | None, Field(description="Calendar title. Defaults to the default calendar.")] = None,
    location: Annotated[str | None, Field(description="Address or place name.")] = None,
    notes: Notes = None,
    all_day: bool = False,
    repeat: Repeat = None,
    repeat_interval: RepeatInterval = 1,
    repeat_days: RepeatDays = None,
    repeat_until: RepeatUntil = None,
    repeat_count: RepeatCount = None,
) -> str:
    """Create a calendar event, optionally recurring."""
    ensure_access(EVENTS)
    event = EventKit.EKEvent.eventWithEventStore_(store)
    event.setTitle_(title)
    event.setStartDate_(to_nsdate(parse_date(start)))
    event.setEndDate_(to_nsdate(parse_date(end)))
    event.setAllDay_(all_day)
    event.setLocation_(location)
    event.setNotes_(notes)
    event.setCalendar_(find_calendar(calendar, EVENTS) if calendar else store.defaultCalendarForNewEvents())
    if repeat:
        event.addRecurrenceRule_(recurrence_rule(repeat, repeat_interval, repeat_days, repeat_until, repeat_count))

    ok, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
    if not ok:
        raise ToolError(error.localizedDescription())
    return f"Created event '{title}' in '{event.calendar().title()}'."


@mcp.tool(title="List reminder lists", annotations=READ_ONLY)
def list_reminder_lists() -> list[Calendar]:
    """List all reminder lists."""
    ensure_access(REMINDERS)
    return describe_calendars(REMINDERS, store.defaultCalendarForNewReminders())


@mcp.tool(title="List reminders", annotations=READ_ONLY)
def list_reminders(
    list_name: Annotated[str | None, Field(description="Only reminders from this list.")] = None,
) -> list[Reminder]:
    """List open reminders, sorted by due date."""
    ensure_access(REMINDERS)
    lists = [find_calendar(list_name, REMINDERS)] if list_name else None
    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, lists)
    (reminders,) = wait_for(store.fetchRemindersMatchingPredicate_completion_, predicate)
    result = [
        {
            "title": r.title() or "",
            "due": from_components(r.dueDateComponents()),
            "list": r.calendar().title(),
            "notes": r.notes(),
            "priority": priority_name(r.priority()),
            "recurring": bool(r.hasRecurrenceRules()),
        }
        for r in reminders or []
    ]
    return sorted(result, key=lambda r: (r["due"] is None, r["due"] or ""))


@mcp.tool(title="Create reminder", annotations=CREATE)
def create_reminder(
    title: str,
    due: Annotated[
        str | None,
        Field(description="Due date (2026-10-17) or datetime (2026-10-17T21:00). A time adds a notification."),
    ] = None,
    list_name: Annotated[str | None, Field(description="List title. Defaults to the default list.")] = None,
    notes: Notes = None,
    priority: Priority = "none",
    repeat: Repeat = None,
    repeat_interval: RepeatInterval = 1,
    repeat_days: RepeatDays = None,
    repeat_until: RepeatUntil = None,
    repeat_count: RepeatCount = None,
) -> str:
    """Create a reminder, optionally recurring. Recurring reminders need a due date."""
    ensure_access(REMINDERS)
    if repeat and not due:
        raise ToolError("Recurring reminders need a due date.")
    reminder = EventKit.EKReminder.reminderWithEventStore_(store)
    reminder.setTitle_(title)
    reminder.setNotes_(notes)
    reminder.setPriority_(PRIORITIES[priority])
    reminder.setCalendar_(find_calendar(list_name, REMINDERS) if list_name else store.defaultCalendarForNewReminders())
    if due:
        reminder.setDueDateComponents_(to_components(due))
        if len(due) > 10:
            reminder.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(to_nsdate(parse_date(due))))
    if repeat:
        reminder.addRecurrenceRule_(recurrence_rule(repeat, repeat_interval, repeat_days, repeat_until, repeat_count))

    ok, error = store.saveReminder_commit_error_(reminder, True, None)
    if not ok:
        raise ToolError(error.localizedDescription())
    return f"Created reminder '{title}' in '{reminder.calendar().title()}'."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
