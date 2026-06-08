"""Minimal event hooks for runtime instrumentation."""

from collections import defaultdict
from collections.abc import Callable

HookHandler = Callable[[str, dict], None]


class HookBus:
    def __init__(self):
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    def on(self, event: str, handler: HookHandler):
        self._handlers[event].append(handler)

    def off(self, event: str, handler: HookHandler):
        handlers = self._handlers.get(event)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._handlers.pop(event, None)

    def emit(self, event: str, payload: dict | None = None, **kwargs):
        data = dict(payload or {})
        data.update(kwargs)
        for handler in list(self._handlers.get(event, [])):
            handler(event, data)
