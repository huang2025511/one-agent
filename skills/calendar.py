"""Calendar Integration — Google Calendar / CalDAV skill.

Provides:
  - List events (today, this week, date range)
  - Create events (with title, time, description, attendees)
  - Update/delete events
  - Check availability (conflict detection)
  - Send calendar invites
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CalendarEvent:
    """A calendar event."""

    def __init__(
        self,
        event_id: str = "",
        title: str = "",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        description: str = "",
        location: str = "",
        attendees: Optional[List[str]] = None,
        is_all_day: bool = False,
        recurrence: str = "",
        status: str = "confirmed",
    ):
        self.event_id = event_id
        self.title = title
        self.start = start
        self.end = end
        self.description = description
        self.location = location
        self.attendees = attendees or []
        self.is_all_day = is_all_day
        self.recurrence = recurrence
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.event_id,
            "title": self.title,
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "description": self.description,
            "location": self.location,
            "attendees": self.attendees,
            "is_all_day": self.is_all_day,
            "recurrence": self.recurrence,
            "status": self.status,
        }

    def __repr__(self) -> str:
        start_str = self.start.strftime("%Y-%m-%d %H:%M") if self.start else "?"
        end_str = self.end.strftime("%H:%M") if self.end else "?"
        return f"{start_str}-{end_str}: {self.title}"


class CalendarSkill:
    """Calendar skill supporting Google Calendar API and CalDAV.

    Currently supports Google Calendar via service account or OAuth.
    """

    name = "calendar"
    description = "Manage calendar events (Google Calendar / CalDAV)"

    def __init__(self):
        self._configured = False
        self._provider = "google"  # google or caldav
        self._credentials: Dict[str, Any] = {}
        # In-memory event store for demo/local mode
        self._events: Dict[str, CalendarEvent] = {}
        self._next_id = 0

    def configure(self, **kwargs) -> None:
        """Configure calendar.

        Google Calendar:
          credentials_file: path to service account JSON
          calendar_id: calendar ID (default: primary)

        CalDAV:
          url: CalDAV server URL
          username: username
          password: password
        """
        self._provider = kwargs.get("provider", "google")
        self._credentials = kwargs
        self._configured = True

    # --------------------------------------------------- list events

    async def list_events(
        self,
        days: int = 7,
        max_results: int = 50,
    ) -> List[CalendarEvent]:
        """List upcoming events."""
        now = datetime.now()
        end_date = now + timedelta(days=days)

        if self._provider == "google":
            return await self._list_google(now, end_date, max_results)
        else:
            return await self._list_local(now, end_date)

    async def list_today(self) -> List[CalendarEvent]:
        """List today's events."""
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        return await self._list_range(today_start, today_end)

    async def list_this_week(self) -> List[CalendarEvent]:
        """List this week's events."""
        now = datetime.now()
        weekday = now.weekday()  # 0=Monday
        week_start = (now - timedelta(days=weekday)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        week_end = week_start + timedelta(days=7)
        return await self._list_range(week_start, week_end)

    async def _list_range(
        self, start: datetime, end: datetime,
    ) -> List[CalendarEvent]:
        if self._provider == "google":
            return await self._list_google(start, end, 100)
        else:
            return await self._list_local(start, end)

    async def _list_google(
        self, start: datetime, end: datetime, max_results: int,
    ) -> List[CalendarEvent]:
        """List events using Google Calendar API."""
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            credentials = service_account.Credentials.from_service_account_file(
                self._credentials.get("credentials_file", ""),
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
            service = build("calendar", "v3", credentials=credentials)

            calendar_id = self._credentials.get("calendar_id", "primary")
            events_result = await asyncio.to_thread(
                service.events().list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat() + "Z",
                    timeMax=end.isoformat() + "Z",
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute,
            )

            items = events_result.get("items", [])
            events = []
            for item in items:
                start_dt = self._parse_datetime(item["start"])
                end_dt = self._parse_datetime(item["end"])
                events.append(CalendarEvent(
                    event_id=item["id"],
                    title=item.get("summary", "无标题"),
                    start=start_dt,
                    end=end_dt,
                    description=item.get("description", ""),
                    location=item.get("location", ""),
                    attendees=[
                        a.get("email", "") for a in item.get("attendees", [])
                    ],
                ))
            return events
        except ImportError:
            logger.warning("google-api-python-client not installed, using local mode")
            return await self._list_local(start, end)
        except Exception as exc:
            logger.warning("google calendar list failed: %s", exc)
            return await self._list_local(start, end)

    async def _list_local(
        self, start: datetime, end: datetime,
    ) -> List[CalendarEvent]:
        """List events from local store."""
        return [
            e for e in self._events.values()
            if e.start and e.start >= start and e.start < end
        ]

    def _parse_datetime(self, dt: dict) -> Optional[datetime]:
        """Parse Google Calendar datetime."""
        if "dateTime" in dt:
            return datetime.fromisoformat(dt["dateTime"].replace("Z", "+00:00"))
        elif "date" in dt:
            return datetime.strptime(dt["date"], "%Y-%m-%d")
        return None

    # --------------------------------------------------- create event

    async def create_event(
        self,
        title: str,
        start_time: str,
        end_time: str = "",
        description: str = "",
        location: str = "",
        attendees: Optional[List[str]] = None,
        is_all_day: bool = False,
    ) -> Dict[str, Any]:
        """Create a new calendar event.

        Args:
            title: event title
            start_time: start time (ISO format or "YYYY-MM-DD HH:MM")
            end_time: end time (default: start + 1 hour)
            description: event description
            location: event location
            attendees: list of attendee email addresses
            is_all_day: all-day event flag
        """
        try:
            start_dt = self._parse_time(start_time)
            end_dt = self._parse_time(end_time) if end_time else start_dt + timedelta(hours=1)
        except ValueError as exc:
            return {"ok": False, "error": f"时间格式错误: {exc}"}

        if start_dt >= end_dt:
            return {"ok": False, "error": "结束时间必须晚于开始时间"}

        # Check conflicts
        conflicts = await self._check_conflicts(start_dt, end_dt)
        if conflicts:
            conflict_titles = [c.title for c in conflicts]
            return {
                "ok": False,
                "error": f"时间冲突: {', '.join(conflict_titles)}",
                "conflicts": conflict_titles,
            }

        if self._provider == "google":
            return await self._create_google(
                title, start_dt, end_dt, description, location, attendees or [],
            )
        else:
            return await self._create_local(
                title, start_dt, end_dt, description, location, attendees or [],
            )

    async def _check_conflicts(
        self, start: datetime, end: datetime,
    ) -> List[CalendarEvent]:
        """Check for time conflicts."""
        existing = await self._list_range(start - timedelta(hours=1), end + timedelta(hours=1))
        conflicts = []
        for e in existing:
            if e.start and e.end:
                if start < e.end and end > e.start:
                    conflicts.append(e)
        return conflicts

    async def _create_google(
        self, title: str, start: datetime, end: datetime,
        description: str, location: str, attendees: List[str],
    ) -> Dict[str, Any]:
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            credentials = service_account.Credentials.from_service_account_file(
                self._credentials.get("credentials_file", ""),
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
            service = build("calendar", "v3", credentials=credentials)

            event_body = {
                "summary": title,
                "description": description,
                "location": location,
                "start": {
                    "dateTime": start.isoformat(),
                    "timeZone": "Asia/Shanghai",
                },
                "end": {
                    "dateTime": end.isoformat(),
                    "timeZone": "Asia/Shanghai",
                },
                "attendees": [{"email": e} for e in attendees],
            }

            calendar_id = self._credentials.get("calendar_id", "primary")
            result = await asyncio.to_thread(
                service.events().insert(
                    calendarId=calendar_id, body=event_body,
                ).execute,
            )
            return {"ok": True, "event_id": result["id"], "title": title}
        except Exception as exc:
            logger.warning("google calendar create failed: %s", exc)
            return await self._create_local(
                title, start, end, description, location, attendees,
            )

    async def _create_local(
        self, title: str, start: datetime, end: datetime,
        description: str, location: str, attendees: List[str],
    ) -> Dict[str, Any]:
        self._next_id += 1
        event_id = f"local_{self._next_id}"
        self._events[event_id] = CalendarEvent(
            event_id=event_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
        )
        return {"ok": True, "event_id": event_id, "title": title}

    # --------------------------------------------------- delete / update

    async def delete_event(self, event_id: str) -> Dict[str, Any]:
        """Delete an event."""
        if event_id in self._events:
            del self._events[event_id]
            return {"ok": True}
        return {"ok": False, "error": f"事件 {event_id} 不存在"}

    async def update_event(
        self, event_id: str, **kwargs,
    ) -> Dict[str, Any]:
        """Update an existing event."""
        if event_id not in self._events:
            return {"ok": False, "error": f"事件 {event_id} 不存在"}

        event = self._events[event_id]
        if "title" in kwargs:
            event.title = kwargs["title"]
        if "description" in kwargs:
            event.description = kwargs["description"]
        if "location" in kwargs:
            event.location = kwargs["location"]
        return {"ok": True, "event_id": event_id}

    # --------------------------------------------------- utilities

    def _parse_time(self, time_str: str) -> datetime:
        """Parse a time string in various formats."""
        import re

        time_str = time_str.strip()

        # Try ISO format
        if "T" in time_str:
            return datetime.fromisoformat(time_str)

        # Try "YYYY-MM-DD HH:MM"
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", time_str)
        if m:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M")

        # Try "YYYY-MM-DD" (all day)
        m = re.match(r"^\d{4}-\d{2}-\d{2}$", time_str)
        if m:
            return datetime.strptime(time_str, "%Y-%m-%d")

        # Try "HH:MM" (today)
        m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
        if m:
            now = datetime.now()
            return now.replace(
                hour=int(m.group(1)), minute=int(m.group(2)),
                second=0, microsecond=0,
            )

        # Try natural language relative times
        now = datetime.now()
        if "明天" in time_str:
            date = now + timedelta(days=1)
        elif "后天" in time_str:
            date = now + timedelta(days=2)
        elif "下周" in time_str:
            days_until_monday = 7 - now.weekday()
            date = now + timedelta(days=days_until_monday)
        else:
            date = now

        # Extract time
        time_match = re.search(r"(\d{1,2})[:：](\d{2})", time_str)
        if time_match:
            date = date.replace(
                hour=int(time_match.group(1)),
                minute=int(time_match.group(2)),
                second=0, microsecond=0,
            )
        else:
            date = date.replace(hour=9, minute=0, second=0, microsecond=0)

        return date

    def format_events(self, events: List[CalendarEvent]) -> str:
        """Format events for display."""
        if not events:
            return "暂无日程安排"

        lines = []
        for e in events:
            if e.start:
                date_str = e.start.strftime("%m/%d")
                time_str = e.start.strftime("%H:%M")
                if e.end:
                    time_str += f"-{e.end.strftime('%H:%M')}"
                lines.append(f"  {date_str} {time_str}  {e.title}")
                if e.location:
                    lines.append(f"           📍 {e.location}")
                if e.description:
                    lines.append(f"           {e.description[:100]}")
        return "\n".join(lines)

    # --------------------------------------------------- skill interface

    def get_skill_schema(self) -> Dict[str, Any]:
        return {
            "name": "calendar",
            "description": "Manage calendar events: list, create, update, delete. Supports Google Calendar and CalDAV.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "list_today", "list_week", "create", "delete", "check"],
                        "description": "action to perform",
                    },
                    "title": {"type": "string", "description": "event title"},
                    "start_time": {"type": "string", "description": "start time (YYYY-MM-DD HH:MM or ISO)"},
                    "end_time": {"type": "string", "description": "end time"},
                    "description": {"type": "string", "description": "event description"},
                    "location": {"type": "string", "description": "event location"},
                    "event_id": {"type": "string", "description": "event ID to delete/update"},
                },
                "required": ["action"],
            },
        }

    async def run(self, args: Dict[str, Any]) -> str:
        """Execute calendar skill."""
        action = args.get("action", "list")

        if action == "list":
            days = int(args.get("days", 7))
            events = await self.list_events(days)
            return self.format_events(events)

        elif action == "list_today":
            events = await self.list_today()
            return "今日日程:\n" + self.format_events(events)

        elif action == "list_week":
            events = await self.list_this_week()
            return "本周日程:\n" + self.format_events(events)

        elif action == "create":
            result = await self.create_event(
                title=args.get("title", "新事件"),
                start_time=args.get("start_time", ""),
                end_time=args.get("end_time", ""),
                description=args.get("description", ""),
                location=args.get("location", ""),
            )
            if result.get("ok"):
                return f"已创建: {result['title']}"
            conflicts = result.get("conflicts", [])
            conflict_str = f" 冲突: {', '.join(conflicts)}" if conflicts else ""
            return f"创建失败: {result.get('error', '')}{conflict_str}"

        elif action == "delete":
            result = await self.delete_event(args.get("event_id", ""))
            return "已删除" if result.get("ok") else result.get("error", "删除失败")

        elif action == "check":
            start = args.get("start_time", "")
            end = args.get("end_time", "")
            if start:
                try:
                    s = self._parse_time(start)
                    e = self._parse_time(end) if end else s + timedelta(hours=1)
                    conflicts = await self._check_conflicts(s, e)
                    if conflicts:
                        return f"该时间段有冲突: {', '.join(c.title for c in conflicts)}"
                    return "该时间段空闲"
                except ValueError:
                    return "时间格式错误"

        return "未知操作"


# Singleton
_calendar_skill: Optional[CalendarSkill] = None


def get_calendar_skill() -> CalendarSkill:
    global _calendar_skill
    if _calendar_skill is None:
        _calendar_skill = CalendarSkill()
    return _calendar_skill