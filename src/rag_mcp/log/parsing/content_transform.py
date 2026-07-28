"""Content transformation module for structured log indexing.

Applies configurable transformation rules to log event text before embedding.
Supports extract, replace, strip, and collapse actions applied in priority order.
"""

from __future__ import annotations

import re

from rag_mcp.log.parsing.config_models import ContentTransformConfig


class ContentTransform:
    """Applies configurable content transformations to log event text.

    Transforms are compiled at initialization and applied in priority order
    (lowest priority value first). Each transform matches against the current
    text state — multiple transforms can modify the same text sequentially.
    """

    def __init__(self, transforms: list[ContentTransformConfig]) -> None:
        """Initialize with a list of transform configurations.

        Args:
            transforms: List of ContentTransformConfig defining the rules.
                Each config specifies a regex pattern, an action, and
                action-specific parameters.
        """
        self._compiled_transforms: list[tuple[ContentTransformConfig, re.Pattern]] = []
        self._compile_transforms(transforms)

    def _compile_transforms(self, transforms: list[ContentTransformConfig]) -> None:
        """Compile regex patterns and sort transforms by priority (lowest first).

        Args:
            transforms: Raw transform configurations to compile.
        """
        compiled = []
        for config in transforms:
            pattern = re.compile(config.match)
            compiled.append((config, pattern))
        # Sort by priority (lowest value = highest priority = applied first)
        compiled.sort(key=lambda item: item[0].priority)
        self._compiled_transforms = compiled

    def transform(self, text: str, captured_groups: dict[str, str] | None = None) -> str:
        """Apply all matching transforms to text in priority order.

        Each compiled transform's regex is tested against the current text.
        If it matches, the corresponding action is applied and the text is
        updated before the next transform is evaluated.

        Args:
            text: The log event text to transform.
            captured_groups: Named groups captured by LineFilter (available
                for potential use in transforms).

        Returns:
            Transformed text ready for embedding. Returns text unchanged
            if no transforms are configured or none match.
        """
        if not self._compiled_transforms:
            return text

        for config, pattern in self._compiled_transforms:
            match = pattern.search(text)
            if match is None:
                continue

            if config.action == "extract":
                text = self._apply_extract(match, config)
            elif config.action == "replace":
                text = self._apply_replace(text, pattern, config)
            elif config.action == "strip":
                text = self._apply_strip(text, pattern)
            elif config.action == "collapse":
                text = self._apply_collapse(text, match, config)

        return text

    def _apply_extract(self, match: re.Match, config: ContentTransformConfig) -> str:
        """Extract only named fields from match, discarding the rest.

        Retains only the values of named capture groups listed in config.fields.
        Values are joined with a space separator.

        Args:
            match: The regex match object.
            config: The transform config with a `fields` list.

        Returns:
            Space-joined values of the extracted named groups.
        """
        if not config.fields:
            return ""

        values = []
        for field_name in config.fields:
            value = match.group(field_name)
            if value is not None:
                values.append(value)
        return " ".join(values)

    def _apply_replace(
        self, text: str, pattern: re.Pattern, config: ContentTransformConfig
    ) -> str:
        """Replace matched portion with configured replacement string.

        Supports regex backreferences (\\1, \\g<name>) via re.sub.

        Args:
            text: The full text being transformed.
            pattern: The compiled regex pattern.
            config: The transform config with a `replacement` string.

        Returns:
            Text with matched portions substituted by the replacement.
        """
        replacement = config.replacement if config.replacement is not None else ""
        return pattern.sub(replacement, text)

    def _apply_strip(self, text: str, pattern: re.Pattern) -> str:
        """Remove matched portion entirely, keeping surrounding text.

        Args:
            text: The full text being transformed.
            pattern: The compiled regex pattern.

        Returns:
            Text with matched portion removed (replaced with empty string).
        """
        return pattern.sub("", text)

    def _apply_collapse(
        self, text: str, match: re.Match, config: ContentTransformConfig
    ) -> str:
        """Truncate matched content to max_length and append annotation.

        If the matched portion's length exceeds max_length, it is truncated
        and the annotation_template is appended (with {byte_count} replaced
        by the number of bytes removed).

        Args:
            text: The full text being transformed.
            match: The regex match object identifying the content to collapse.
            config: The transform config with `max_length` and
                optional `annotation_template`.

        Returns:
            Text with the matched portion collapsed if it exceeds max_length,
            or unchanged if the matched portion is within the limit.
        """
        matched_text = match.group(0)
        max_length = config.max_length if config.max_length is not None else len(matched_text)

        if len(matched_text) <= max_length:
            return text

        truncated = matched_text[:max_length]
        byte_count = len(matched_text) - max_length

        annotation = ""
        if config.annotation_template:
            annotation = config.annotation_template.format(byte_count=byte_count)

        replacement = truncated + annotation
        # Replace only the first occurrence of the matched text
        return text[: match.start()] + replacement + text[match.end() :]
