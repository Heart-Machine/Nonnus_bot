"""What a preparation is doing right now, for the status messages of whoever
is waiting on it.

A preparation reports stages - waiting for a slot, downloading so many of so
many files, compressing, uploading - without knowing who listens or how it
is shown. Once the post has been looked at, the download says what it is -
a reel, a photo, a carousel - so that the status can name it.

The download itself runs in a worker thread, so it does not get a reporter
passed in: it calls report(), which finds the Progress of the preparation it
runs for through a context variable. asyncio.to_thread copies the context
into the thread, so that needs no plumbing through signatures, and code that
runs outside any preparation - a test, say - reports into nothing.
"""
import asyncio
from contextvars import ContextVar
from typing import Callable, Optional

QUEUED = "queued"
DOWNLOADING = "downloading"
COMPRESSING = "compressing"
UPLOADING = "uploading"

# What the post turned out to be. Not known before Instagram has been asked -
# the link does not say whether a /p/ post is a photo, a video or a carousel -
# so the stages before that report none.
REEL = "reel"
PHOTO = "photo"
CAROUSEL = "carousel"
# Known from the link itself, the one exception: whatever is in them.
STORY = "story"
HIGHLIGHT = "highlight"

Listener = Callable[[str, int, int, Optional[str]], None]


class Progress:
    """The latest stage of one preparation, and who to tell about the next.

    Listeners are called on the event loop, whatever thread the report came
    from. One joining partway through - a second person asking for the same
    post - hears the current stage at once."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._listeners: list[Listener] = []
        self.stage: Optional[tuple[str, int, int, Optional[str]]] = None

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)
        if self.stage is not None:
            listener(*self.stage)

    def report(self, stage: str, done: int = 0, total: int = 0, kind: Optional[str] = None) -> None:
        # Safe from any thread: the listeners run on the loop either way.
        self._loop.call_soon_threadsafe(self._publish, (stage, done, total, kind))

    def _publish(self, stage: tuple[str, int, int, Optional[str]]) -> None:
        self.stage = stage
        for listener in list(self._listeners):
            listener(*stage)


CURRENT: ContextVar[Optional[Progress]] = ContextVar("nonnus_progress", default=None)


def report(stage: str, done: int = 0, total: int = 0, kind: Optional[str] = None) -> None:
    """Report a stage of the preparation this code runs for, if any."""
    current = CURRENT.get()
    if current is not None:
        current.report(stage, done, total, kind)
