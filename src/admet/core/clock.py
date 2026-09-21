"""One way to stamp the time, for everything an operator reads.

Kept apart from the UTC stamps in session.py, which record when a project file
was written. These are wall-clock with the local offset, because they are read
next to a rig by somebody deciding whether what they are looking at is now.
"""

from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    """Local time to the millisecond, with its offset."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
