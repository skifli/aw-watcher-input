from __future__ import annotations

import glob
import os
import select
import struct
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


_LONG_SIZE = struct.calcsize("l")
_EVENT_STRUCT = "qqHHi" if _LONG_SIZE == 8 else "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_STRUCT)

EV_KEY = 0x01
EV_REL = 0x02

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08

MOUSE_BUTTON_CODE_MIN = 272
MOUSE_BUTTON_CODE_MAX = 279


@dataclass
class _Record:
    seq: int
    kind: str
    data: Dict[str, int]


class LinuxInputMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._refcount = 0
        self._fds: Dict[int, str] = {}
        self._seq = 0
        self._events: Deque[_Record] = deque(maxlen=4096)

    def start(self):
        with self._lock:
            self._refcount += 1
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._open_devices()
            self._thread = threading.Thread(
                target=self._run, name="linux-input-monitor", daemon=True
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            self._refcount = max(0, self._refcount - 1)
            if self._refcount > 0:
                return
            self._stop.set()
            for fd in list(self._fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds.clear()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _open_devices(self):
        self._fds.clear()
        for path in glob.glob("/dev/input/event*"):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self._fds[fd] = path

    def _record(self, kind: str, data: Dict[str, int]):
        if not data:
            return
        with self._lock:
            self._seq += 1
            self._events.append(_Record(self._seq, kind, data))

    def _decode(self, event_type: int, code: int, value: int):
        if event_type == EV_KEY:
            if value != 1:
                return None
            if MOUSE_BUTTON_CODE_MIN <= code <= MOUSE_BUTTON_CODE_MAX:
                return "mouse", {"clicks": 1}
            return "keyboard", {"presses": 1}
        if event_type != EV_REL:
            return None
        data = {"clicks": 0, "deltaX": 0, "deltaY": 0, "scrollX": 0, "scrollY": 0}
        if code == REL_X:
            data["deltaX"] = abs(value)
        elif code == REL_Y:
            data["deltaY"] = abs(value)
        elif code == REL_HWHEEL:
            data["scrollX"] = abs(value)
        elif code == REL_WHEEL:
            data["scrollY"] = abs(value)
        else:
            return None
        return "mouse", data

    def _run(self):
        while not self._stop.is_set():
            if not self._fds:
                self._open_devices()
                if not self._fds:
                    self._stop.wait(1.0)
                    continue
            try:
                ready, _, _ = select.select(list(self._fds.keys()), [], [], 1.0)
            except (OSError, ValueError):
                ready = []
            for fd in ready:
                try:
                    chunk = os.read(fd, _EVENT_SIZE * 64)
                except OSError as e:
                    if e.errno in (19, 9):
                        self._fds.pop(fd, None)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    
                    continue
                for offset in range(0, len(chunk) - (len(chunk) % _EVENT_SIZE), _EVENT_SIZE):
                    _, _, event_type, code, value = struct.unpack(
                        _EVENT_STRUCT, chunk[offset : offset + _EVENT_SIZE]
                    )
                    decoded = self._decode(event_type, code, value)
                    if decoded is None:
                        continue
                    kind, data = decoded
                    self._record(kind, data)

    def snapshot(self, kind: str, cursor: int):
        with self._lock:
            if kind == "keyboard":
                data = {"presses": 0}
            else:
                data = {"clicks": 0, "deltaX": 0, "deltaY": 0, "scrollX": 0, "scrollY": 0}
            new_cursor = cursor
            for record in self._events:
                if record.seq <= cursor or record.kind != kind:
                    continue
                for key, value in record.data.items():
                    data[key] = data.get(key, 0) + value
                new_cursor = record.seq
            return data, new_cursor, new_cursor != cursor


_MONITOR = LinuxInputMonitor()


def start_monitor():
    _MONITOR.start()


def stop_monitor():
    _MONITOR.stop()


def monitor_alive() -> bool:
    return _MONITOR.alive()


def snapshot(kind: str, cursor: int):
    return _MONITOR.snapshot(kind, cursor)
