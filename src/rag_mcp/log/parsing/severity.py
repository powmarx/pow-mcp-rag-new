"""Severity normalization for structured log indexing.

Maps raw severity prefixes (e.g., ERR, WRN, DBG) to normalized levels:
debug, info, warning, error, critical.
"""

# Default mapping from raw severity prefixes to normalized levels.
# Covers common device log prefixes and standard syslog-style levels.
DEFAULT_SEVERITY_MAP: dict[str, str] = {
    # Device log prefixes (3-letter codes)
    "ERR": "error",
    "WRN": "warning",
    "DBG": "debug",
    # Standard syslog / common log levels
    "CRITICAL": "critical",
    "FATAL": "critical",
    "ERROR": "error",
    "WARN": "warning",
    "WARNING": "warning",
    "INFO": "info",
    "DEBUG": "debug",
    "TRACE": "debug",
}


def normalize_severity(raw: str, custom_mapping: dict[str, str] | None = None) -> str:
    """Normalize a raw severity string to one of: debug, info, warning, error, critical.

    Uses custom_mapping first (if provided), falls back to DEFAULT_SEVERITY_MAP.
    Returns 'info' if no mapping found.

    Args:
        raw: The raw severity string from the log line (e.g., "ERR", "WRN", "debug").
        custom_mapping: Optional project-specific severity mapping that takes precedence
            over the default map. Keys can be any case; lookup tries both the original
            value and upper-cased version.

    Returns:
        One of: "debug", "info", "warning", "error", "critical".
    """
    if not raw or not raw.strip():
        return "info"

    upper = raw.strip().upper()

    if custom_mapping:
        # Try exact match first, then upper-cased match
        result = custom_mapping.get(raw.strip()) or custom_mapping.get(upper)
        if result:
            return result

    return DEFAULT_SEVERITY_MAP.get(upper, "info")
