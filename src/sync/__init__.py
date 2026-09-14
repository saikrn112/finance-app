"""Multi-device sync: per-device payloads merged per record.

See docs/multi_device_sync.md for the design and for why the vault backup cannot do this job.

Importing this package installs the tombstone flush hook, which is why `tracking` is imported here
rather than lazily: a delete that happens before the hook is installed leaves no tombstone and will
be undone by the next merge.
"""
from src.sync import tracking  # noqa: F401  (imported for its event registration)

__all__ = ["tracking"]
