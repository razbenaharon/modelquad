# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Keep the Codex quota reading fresh in the background, for display only.

Codex quota comes only from the app-server (``account/rateLimits/read``), and
that probe can take up to 45 seconds. The status line and the handoff
suggestion read the local cache instead of probing. The long-lived MCP server
keeps that cache fresh from a daemon thread -- once at start, then every
``CODEX_REFRESH_INTERVAL_SECONDS``, a little inside the age at which a reading
stops being fresh, so the display never flickers to stale between cycles.

Why the MCP server and not the status-line command: the status line runs
every few seconds in a short-lived process, so refreshing from there would
mean launching a detached, windowless child process again and again. The
MCP server already lives for the whole Claude Code session.

The loop only calls ``usage.refresh.refresh_provider('codex', ...)``, which
owns the cross-process debounce and lock, so several Claude Code sessions do
not probe more than once per interval between them. It starts no worker,
changes no routing and writes nothing but the usage snapshot. When the
app-server cannot be reached, the snapshot says so and every percentage stays
unknown; nothing is shown as 0%.

Set ``MODELQUAD_CODEX_QUOTA_REFRESH=0`` to turn the background probe off; it
is checked before every cycle. :func:`stop_codex_refresh` stops the loop.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from ..disclosure import notice_shown
from .refresh import FRESH_SECONDS, refresh_provider

logger = logging.getLogger(__name__)

#: How often the background loop asks Codex: inside FRESH_SECONDS, so a
#: reading is replaced before it turns stale.
CODEX_REFRESH_INTERVAL_SECONDS = FRESH_SECONDS - 60.0
#: The cross-process debounce the loop asks for: just under the interval, so
#: this session's own cycle is never skipped by jitter, while a reading
#: another session took moments ago still is.
CODEX_REFRESH_MIN_INTERVAL_SECONDS = CODEX_REFRESH_INTERVAL_SECONDS - 10.0

#: Environment switch: "0", "false", "no" or "off" turns the probe off.
REFRESH_SWITCH = "MODELQUAD_CODEX_QUOTA_REFRESH"

THREAD_NAME = "codex-quota-refresh"

_running: tuple[Thread, Event] | None = None
_running_lock = Lock()


def refresh_enabled() -> bool:
    """Whether the background Codex quota probe may run this cycle."""
    return os.environ.get(REFRESH_SWITCH, "").strip().casefold() not in {"0", "false", "no", "off"}


def run_codex_refresh_loop(
    usage_directory: Path | str,
    stop: Event,
    *,
    interval: float = CODEX_REFRESH_INTERVAL_SECONDS,
    refresh: Callable[..., Any] | None = None,
) -> None:
    """Refresh Codex quota now, then every ``interval`` seconds, until ``stop``.

    A failed probe is logged and the loop keeps going; it never raises. A
    cycle is skipped while the environment switch is off, and until the
    data-flow notice, which describes this probe, has reached the user: a
    plugin install can start the server before anything printed it.
    """

    if refresh is None:
        refresh = functools.partial(refresh_provider, min_interval=CODEX_REFRESH_MIN_INTERVAL_SECONDS)
    while not stop.is_set():
        if refresh_enabled() and notice_shown():
            try:
                refresh("codex", Path(usage_directory))
            except Exception as exc:  # noqa: BLE001 -- a background refresh must never crash the server
                logger.debug("background Codex quota refresh failed: %s", type(exc).__name__)
        stop.wait(interval)


def start_codex_refresh(
    usage_directory: Path | str,
    *,
    interval: float = CODEX_REFRESH_INTERVAL_SECONDS,
    refresh: Callable[..., Any] | None = None,
) -> tuple[Thread, Event]:
    """Start the background loop on a daemon thread and return at once.

    The thread is a daemon, so it never holds the server open at exit; set the
    returned event, or call :func:`stop_codex_refresh`, to stop it.
    """

    global _running
    stop = Event()
    thread = Thread(
        target=run_codex_refresh_loop,
        args=(usage_directory, stop),
        kwargs={"interval": interval, "refresh": refresh},
        daemon=True,
        name=THREAD_NAME,
    )
    thread.start()
    with _running_lock:
        _running = (thread, stop)
    return thread, stop


def stop_codex_refresh(timeout: float | None = None) -> bool:
    """Stop the loop :func:`start_codex_refresh` started; True when none is left running.

    A probe already in flight finishes first; ``timeout`` bounds the wait.
    """

    global _running
    with _running_lock:
        running, _running = _running, None
    if running is None:
        return True
    thread, stop = running
    stop.set()
    thread.join(timeout)
    return not thread.is_alive()
