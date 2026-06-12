"""JSONL session transcript."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

_MAX_STR = 2000


def _clip(value: Any) -> Any:
    """Bound long strings so a huge eval result cannot bloat the transcript."""
    if isinstance(value, str) and len(value) > _MAX_STR:
        return value[:_MAX_STR] + f"...[clipped, {len(value)} chars total]"
    return value


def log_event(session_dir: Path, event: str, data: dict[str, Any]) -> None:
    """Append a timestamped event to the session's log/transcript.jsonl.

    Logging must never break the actual command, so failures are swallowed.
    """
    try:
        log_dir = session_dir / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "event": event,
            **{k: _clip(v) for k, v in data.items()},
        }
        with (log_dir / "transcript.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
