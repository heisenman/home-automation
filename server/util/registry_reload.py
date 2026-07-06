"""Live registry reload for the long-running ingest bridges (relocate-ingest-reload, 2026-07-06).

The ingest bridges (scanner, edge_mapper, tasmota_bridge, levoit_bridge, edge_history) load the device
registry ONCE at startup and stamp every reading's ``area`` from that cached copy. A UI device-relocate
edits ``devices.yaml`` but a bridge kept using its cached copy, so the very next reading re-stamped the
OLD area into ``device_last_seen`` and the device "popped back" within one scan cycle.

``RegistryReloader`` wraps a ``(path, loader)`` pair and hands out the freshest registry via
``.current()``, reloading via the bridge's own ``load_registry`` when the file's mtime changes. So a
relocate takes effect on live ingest within one poll — no restart, no cross-process signal.

Robustness: ``device_relocate`` rewrites the registry file non-atomically (``path.write_text``), so a
poll can catch a torn read mid-write. A failed reload keeps BOTH the previous value and the previous
mtime, so the next poll retries — the update is never silently lost. ``stat()`` is throttled to at most
once per ``throttle_s`` so a high-frequency hot path (BLE adverts) doesn't hammer the filesystem.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("ha.ingest.registry_reload")


class RegistryReloader:
    """mtime-watching registry cache. Call ``.current()`` on the hot path for the freshest registry."""

    def __init__(self, path, loader: Callable[[Path], dict], *,
                 throttle_s: float = 2.0, logger: logging.Logger | None = None):
        self._path = Path(path)
        self._loader = loader
        self._throttle_s = throttle_s
        self._log = logger or log
        self._value = loader(self._path)
        self._mtime = self._stat()
        self._next_check = 0.0

    def _stat(self):
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def current(self) -> dict:
        now = time.monotonic()
        if now < self._next_check:
            return self._value
        self._next_check = now + self._throttle_s
        m = self._stat()
        if m is not None and m != self._mtime:
            try:
                value = self._loader(self._path)
            except Exception:
                # torn read (relocate rewriting the file) or transient error: keep the previous value
                # AND the previous mtime so the next poll retries — never lose the update.
                self._log.exception("registry reload failed for %s — keeping previous (will retry)", self._path)
                return self._value
            self._value = value
            self._mtime = m
            self._log.info("registry reloaded: %s changed → %d entries", self._path, len(value))
        return self._value
