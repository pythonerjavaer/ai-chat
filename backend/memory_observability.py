"""Low-overhead process RSS checkpoints for production incident diagnosis."""

from __future__ import annotations

import logging
import os
import resource
from logging import Logger
from pathlib import Path
from typing import Any


def process_rss_mb() -> float:
    """Return current RSS on Linux and the best available value elsewhere."""
    try:
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux reports KiB. macOS reports bytes.
        return value / (1024 if os.uname().sysname == "Linux" else 1024 * 1024)


def log_memory_checkpoint(
    logger: Logger,
    operation: str,
    phase: str,
    *,
    before_mb: float | None = None,
    **fields: Any,
) -> float:
    """Log one structured checkpoint without enabling a heavy profiler."""
    rss_mb = process_rss_mb()
    delta = None if before_mb is None else rss_mb - before_mb
    details = " ".join(
        f"{key}={value}" for key, value in fields.items()
        if value is not None
    )
    # Uvicorn config does not necessarily attach an INFO handler to arbitrary
    # application loggers. Reuse its error logger when available so Render
    # actually retains these normal (non-warning) checkpoints.
    target = logging.getLogger("uvicorn.error")
    if not target.isEnabledFor(logging.INFO):
        target = logger
    target.info(
        "memory_checkpoint operation=%s phase=%s memory_rss_mb=%.1f "
        "memory_delta_mb=%s%s",
        operation,
        phase,
        rss_mb,
        "n/a" if delta is None else f"{delta:.1f}",
        f" {details}" if details else "",
    )
    return rss_mb
