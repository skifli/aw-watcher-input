"""
Listeners for aggregated keyboard and mouse events.

This is used for AFK detection on Linux, as well as used in aw-watcher-input to track input activity in general.

NOTE: Logging usage should be commented out before committed, for performance reasons.
"""

import logging
import threading
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from typing import Dict, Any, List

from .linux_input import monitor_alive, snapshot as linux_snapshot, start_monitor, stop_monitor

logger = logging.getLogger(__name__)
# logger.setLevel(logging.DEBUG)


class EventFactory(metaclass=ABCMeta):
    def __init__(self) -> None:
        self.new_event = threading.Event()
        self._reset_data()

    @abstractmethod
    def _reset_data(self) -> None:
        self.event_data: Dict[str, Any] = {}

    def next_event(self) -> dict:
        """Returns an event and prepares the internal state so that it can start to build a new event"""
        self.new_event.clear()
        data = self.event_data
        # self.logger.debug(f"Event: {data}")
        self._reset_data()
        return data

    def has_new_event(self) -> bool:
        return self.new_event.is_set()


class KeyboardListener(EventFactory):
    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("keyboard")
        self._cursor = 0

    def start(self):
        start_monitor()

    def stop(self):
        stop_monitor()

    def is_alive(self) -> bool:
        return monitor_alive()

    def _reset_data(self):
        self.event_data = {"presses": 0}

    def on_press(self, key):
        # self.logger.debug(f"Press: {key}")
        self.event_data["presses"] += 1
        self.new_event.set()

    def on_release(self, key):
        # Don't count releases, only clicks
        # self.logger.debug(f"Release: {key}")
        pass

    def next_event(self) -> dict:
        data, self._cursor, _ = linux_snapshot("keyboard", self._cursor)
        self._reset_data()
        self.event_data.update(data)
        return data

    def has_new_event(self) -> bool:
        _, _, has_event = linux_snapshot("keyboard", self._cursor)
        return has_event


class MouseListener(EventFactory):
    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("mouse")
        self.pos = None
        self._cursor = 0

    def _reset_data(self):
        self.event_data = defaultdict(int)
        self.event_data.update(
            {"clicks": 0, "deltaX": 0, "deltaY": 0, "scrollX": 0, "scrollY": 0}
        )

    def start(self):
        start_monitor()

    def stop(self):
        stop_monitor()

    def is_alive(self) -> bool:
        return monitor_alive()

    def on_move(self, x, y):
        newpos = (x, y)
        # self.logger.debug("Moved mouse to: {},{}".format(x, y))
        if not self.pos:
            self.pos = newpos

        delta = tuple(self.pos[i] - newpos[i] for i in range(2))
        self.event_data["deltaX"] += abs(delta[0])
        self.event_data["deltaY"] += abs(delta[1])

        self.pos = newpos
        self.new_event.set()

    def on_click(self, x, y, button, down):
        # self.logger.debug(f"Click: {button} at {(x, y)}")
        # Only count presses, not releases
        if down:
            self.event_data["clicks"] += 1
            self.new_event.set()

    def on_scroll(self, x, y, scrollx, scrolly):
        # self.logger.debug(f"Scroll: {scrollx}, {scrolly} at {(x, y)}")
        self.event_data["scrollX"] += abs(scrollx)
        self.event_data["scrollY"] += abs(scrolly)
        self.new_event.set()

    def next_event(self) -> dict:
        data, self._cursor, _ = linux_snapshot("mouse", self._cursor)
        self._reset_data()
        self.event_data.update(data)
        return data

    def has_new_event(self) -> bool:
        _, _, has_event = linux_snapshot("mouse", self._cursor)
        return has_event


class GamepadListener(EventFactory):
    """Optional gamepad listener.

    This vendored build does not depend on an external gamepad package, so the
    listener stays disabled unless a local implementation is added later.
    """

    def __init__(self):
        EventFactory.__init__(self)
        self.logger = logger.getChild("gamepad")
        self._threads = []
        self._devices = []
        self._stop_event = threading.Event()

    def _reset_data(self):
        self.event_data = {"buttons": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self.logger.debug("Gamepad listener disabled in vendored build")
        self._stop_event.clear()

    def stop(self):
        self._stop_event.set()
        self._devices.clear()
        self._threads.clear()

    def is_alive(self) -> bool:
        return False
