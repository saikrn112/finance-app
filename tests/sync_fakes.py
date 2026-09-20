"""A transport test double.

Dict-backed, so the convergence and soak suites exercise the real publish/fetch/merge path with no
network, no credentials and no filesystem. Replaces the directory-based transport that used to live in
`src/sync/transport.py` and was removed: a folder only reaches processes that can see that filesystem,
so shipping one made single-machine sync look like multi-device sync.

Payloads are round-tripped through JSON on the way in and out, which is not incidental -- it is what
catches a value that cannot be serialised, or that comes back a different type. A fake that handed the
same dict object back would hide exactly that.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from src.sync.transport import RemotePayload


class FakeTransport:
    """Holds one payload per device, as Drive does."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.modified: dict[str, datetime] = {}
        self.put_count = 0
        #: Set to raise from `fetch_others`, to test that a transport failure is survivable.
        self.fail_fetch: Exception | None = None
        self._clock = datetime(2026, 1, 1)

    def put(self, device_id: str, payload: dict) -> None:
        self.files[device_id] = json.dumps(payload, default=str)
        self._clock += timedelta(seconds=1)
        self.modified[device_id] = self._clock
        self.put_count += 1

    def fetch_others(self, device_id: str) -> list[RemotePayload]:
        if self.fail_fetch is not None:
            raise self.fail_fetch
        out: list[RemotePayload] = []
        for peer, raw in sorted(self.files.items()):
            if peer == device_id:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Mirrors the real transport: one unreadable peer must not stop the others.
                continue
            out.append(RemotePayload(peer, payload, self.modified.get(peer)))
        return out

    def delete(self, device_id: str) -> None:
        self.files.pop(device_id, None)
        self.modified.pop(device_id, None)

    def has_payload(self, device_id: str) -> bool:
        return device_id in self.files

    def describe(self) -> str:
        return "fake transport"

    # -- helpers for tests ----------------------------------------------------------------------

    def corrupt(self, device_id: str) -> None:
        """Leave an unparseable payload, as a half-finished upload would."""
        self.files[device_id] = '{"records": {"trans'

    def raw(self, device_id: str) -> dict:
        return json.loads(self.files[device_id])
