from datetime import datetime, timezone, timedelta
from typing import Any


def _timestamp_parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Event(dict):
    def __init__(self, timestamp: datetime, duration: float = 0, data=None, id=None):
        super().__init__()
        if data is None:
            data = {}
        self["id"] = id
        self["timestamp"] = timestamp
        self["duration"] = duration
        self["data"] = data

    @property
    def id(self):
        return self["id"]

    @property
    def data(self):
        return self["data"]

    @property
    def timestamp(self):
        return self["timestamp"]

    @property
    def duration(self):
        return self["duration"]

    def to_json_dict(self):
        dur = self.duration
        if isinstance(dur, timedelta):
            dur = dur.total_seconds()
        return {"timestamp": self.timestamp.isoformat(), "duration": dur, "data": self.data}

    def to_json_str(self):
        import json

        return json.dumps(self.to_json_dict())

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Event):
            return False
        return self.to_json_dict() == other.to_json_dict() and self.id == other.id

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self.timestamp < other.timestamp

