from collections import deque

from .exceptions import Empty


class FIFOSQLiteQueue:
    def __init__(self, path, multithreading=True, auto_commit=False):
        self._items = deque()

    def put(self, item):
        self._items.append(item)

    def get(self, block=False):
        if not self._items:
            raise Empty()
        return self._items.popleft()

    def task_done(self):
        return None

    def qsize(self):
        return len(self._items)
