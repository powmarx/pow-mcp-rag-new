"""
Timestamp helpers for log metadata.

ChromaDB only supports ``$gte``/``$lte`` on int/float metadata, so log chunks
carry their time range twice: as ISO 8601 strings (for display) and as epoch
seconds (for filtering). This module is the single place that converts
between the two.
"""

from datetime import datetime, timezone

# Metadata keys holding the numeric (epoch seconds, UTC) form of the chunk's
# time range. Kept alongside the ISO string fields
# ``timestamp_range_start`` / ``timestamp_range_end``.
TS_START_EPOCH_KEY = "ts_start_epoch"
TS_END_EPOCH_KEY = "ts_end_epoch"


def iso_to_epoch(value: str | None) -> float | None:
    """Convert an ISO 8601 timestamp to epoch seconds (UTC).

    Accepts the forms produced by the log parser (``2024-01-15T10:30:00Z``)
    as well as offsets (``+03:00``), fractional seconds, a space instead of
    ``T``, and a bare date (``2024-01-15`` → midnight UTC). Naive values are
    treated as UTC.

    Returns None for empty or unparseable input; never raises.
    """
    if not value or not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.endswith(("Z", "z")):
        stripped = stripped[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(stripped)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def epoch_fields(timestamp_start: str | None, timestamp_end: str | None) -> dict:
    """Build the numeric metadata fields for a chunk's time range.

    Only keys whose timestamp parses are included, so a chunk without a
    usable timestamp simply has no epoch fields (and is excluded from
    time-range queries rather than matched spuriously).
    """
    fields: dict = {}
    start = iso_to_epoch(timestamp_start)
    end = iso_to_epoch(timestamp_end)
    if start is not None:
        fields[TS_START_EPOCH_KEY] = start
    if end is not None:
        fields[TS_END_EPOCH_KEY] = end
    return fields


def parse_time_filter(value: str, param_name: str) -> float:
    """Parse a user-supplied time filter, raising ValueError with a clear message."""
    epoch = iso_to_epoch(value)
    if epoch is None:
        raise ValueError(
            f"{param_name} must be an ISO 8601 timestamp "
            f"(e.g. 2026-04-28T05:00:00Z), got '{value}'."
        )
    return epoch
