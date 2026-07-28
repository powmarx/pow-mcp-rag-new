"""
Configuration loader for the RAG MCP Server.

Reads and validates config.yaml, providing typed dataclasses for all settings.
Shared between the indexer CLI and the MCP server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rag_mcp.log.parsing.config_models import (
    LogPatternConfig,
    LogSettings,
)

# Directories skipped during discovery and indexing. Overridable via the
# top-level `excluded_dirs` key in config.yaml.
DEFAULT_EXCLUDED_DIRS = [
    "node_modules", "venv", ".venv", "bin", "obj",
    "build", "dist", "__pycache__", ".vs", ".git",
]


@dataclass
class SourcePattern:
    """A file pattern to index within a project."""

    pattern: str
    type: str  # "header", "source", "documentation", "config", "log"
    description: str = ""
    log_patterns: list[LogPatternConfig] = field(default_factory=list)


@dataclass
class IndexExtension:
    """A file extension that discovery turns into a recursive source pattern."""

    ext: str  # e.g. ".cpp" (leading dot, lowercase)
    type: str  # "source", "header", "documentation"
    description: str = ""


# Default file types discovery indexes: code (C/C++, Python, React/JS/TS, C#,
# Go, Kotlin) and docs/specs (Markdown, PDF, text). Overridable via the
# top-level `index_extensions` key in config.yaml. Order is preserved.
DEFAULT_INDEX_EXTENSIONS = [
    IndexExtension(".c", "source", "C sources"),
    IndexExtension(".h", "header", "C/C++ headers"),
    IndexExtension(".cpp", "source", "C++ sources"),
    IndexExtension(".cc", "source", "C++ sources"),
    IndexExtension(".cxx", "source", "C++ sources"),
    IndexExtension(".hpp", "header", "C++ headers"),
    IndexExtension(".hh", "header", "C++ headers"),
    IndexExtension(".hxx", "header", "C++ headers"),
    IndexExtension(".py", "source", "Python sources"),
    IndexExtension(".cs", "source", "C# sources"),
    IndexExtension(".go", "source", "Go sources"),
    IndexExtension(".kt", "source", "Kotlin sources"),
    IndexExtension(".kts", "source", "Kotlin scripts"),
    IndexExtension(".js", "source", "JavaScript sources"),
    IndexExtension(".jsx", "source", "React JSX sources"),
    IndexExtension(".ts", "source", "TypeScript sources"),
    IndexExtension(".tsx", "source", "React TSX sources"),
    IndexExtension(".md", "documentation", "Markdown docs/specs"),
    IndexExtension(".pdf", "documentation", "PDF docs/specs"),
    IndexExtension(".txt", "documentation", "Text docs/specs"),
]


@dataclass
class ProjectConfig:
    """Configuration for a single project to index."""

    name: str
    description: str
    base_path: str  # Resolved absolute path
    sources: list[SourcePattern] = field(default_factory=list)
    log_settings: LogSettings | None = None
    auto_reindex: bool = True  # If False, skip this project during background reindex
    removed: bool = False  # If True, project is soft-deleted (data cleared, config preserved)


@dataclass
class ChunkingConfig:
    """Text chunking configuration."""

    chunk_size: int = 1000
    chunk_overlap: int = 200
    separators: list[str] = field(
        default_factory=lambda: ["\n## ", "\n### ", "\n\n", "\n"]
    )


@dataclass
class StorageConfig:
    """ChromaDB storage configuration."""

    path: str = "./data"
    collection_prefix: str = "rag"
    mode: str = "local"  # "local" or "remote" (Phase 2)
    url: str = ""  # Remote ChromaDB URL (Phase 2)


@dataclass
class EmbeddingConfig:
    """Embedding model configuration."""

    model: str = "BAAI/bge-small-en-v1.5"
    # Some models (e.g. BGE) score noticeably better on retrieval when queries
    # are prefixed with an instruction (documents are NOT prefixed). Left
    # empty for models that don't need it (e.g. MiniLM).
    query_instruction: str = "Represent this sentence for searching relevant passages: "


@dataclass
class RerankerConfig:
    """Cross-encoder reranking configuration.

    When enabled, search_docs over-fetches `top_k * overfetch_factor` candidates
    from ChromaDB's bi-encoder vector search, then reorders them using a
    cross-encoder model before truncating to top_k. This is a second-stage
    reranking pass — it requires no reindex since it operates on already
    retrieved candidates.
    """

    enabled: bool = True
    model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    overfetch_factor: int = 4


@dataclass
class AppConfig:
    """Top-level application configuration."""

    embedding: EmbeddingConfig
    storage: StorageConfig
    chunking: ChunkingConfig
    projects: list[ProjectConfig]
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    discovery_ignore: list[str] = field(default_factory=list)
    excluded_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED_DIRS))
    index_extensions: list[IndexExtension] = field(
        default_factory=lambda: list(DEFAULT_INDEX_EXTENSIONS)
    )


class ConfigLoader:
    """Loads and validates config.yaml."""

    def __init__(self, config_path: Path):
        self.config_path = config_path

    @staticmethod
    def _validate_regex(pattern: str, context: str) -> None:
        """Validate that a regex string is compilable by Python's re module.

        Args:
            pattern: The regex string to validate.
            context: A descriptive string identifying the entry (for error messages).

        Raises:
            ValueError: If the regex cannot be compiled.
        """
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(
                f"Invalid regex in {context}: {e}. Pattern: '{pattern}'"
            ) from e

    def load(self) -> AppConfig:
        """Load config from YAML file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not raw:
            raise ValueError(f"Config file is empty: {self.config_path}")

        return self._parse_config(raw)

    def save(self, config: AppConfig) -> None:
        """Write config back to YAML (used by --add-project)."""
        data = self._serialize_config(config)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def expand_path(self, path: str) -> str:
        """Expand ~ and ${VAR_NAME} environment variables in paths. Returns resolved absolute path."""
        import os
        import re

        # Expand ${VAR_NAME} syntax
        def _replace_env(match):
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                print(
                    f"  [warning] Environment variable not set: ${{{var_name}}}",
                    file=__import__("sys").stderr,
                )
                return match.group(0)  # Leave unexpanded
            return value

        expanded = re.sub(r"\$\{([^}]+)\}", _replace_env, path)

        # Expand ~ for home directory
        expanded_path = Path(expanded).expanduser()
        return str(expanded_path.resolve())

    def _parse_config(self, raw: dict[str, Any]) -> AppConfig:
        """Parse raw YAML dict into typed AppConfig."""
        # Embedding config
        embedding_raw = raw.get("embedding", {})
        embedding = EmbeddingConfig(
            model=embedding_raw.get("model", EmbeddingConfig.model),
            query_instruction=embedding_raw.get(
                "query_instruction", EmbeddingConfig.query_instruction
            ),
        )

        # Storage config
        storage_raw = raw.get("storage", {})
        storage = StorageConfig(
            path=storage_raw.get("path", StorageConfig.path),
            collection_prefix=storage_raw.get(
                "collection_prefix", StorageConfig.collection_prefix
            ),
            mode=storage_raw.get("mode", StorageConfig.mode),
            url=storage_raw.get("url", StorageConfig.url),
        )

        # Chunking config
        chunking_raw = raw.get("chunking", {})
        chunking = ChunkingConfig(
            chunk_size=chunking_raw.get("chunk_size", ChunkingConfig.chunk_size),
            chunk_overlap=chunking_raw.get("chunk_overlap", ChunkingConfig.chunk_overlap),
            separators=chunking_raw.get("separators", None)
            or ChunkingConfig().separators,
        )

        # Reranker config
        reranker_raw = raw.get("reranker", {})
        reranker = RerankerConfig(
            enabled=reranker_raw.get("enabled", RerankerConfig.enabled),
            model=reranker_raw.get("model", RerankerConfig.model),
            overfetch_factor=reranker_raw.get("overfetch_factor", RerankerConfig.overfetch_factor),
        )

        # Projects
        projects_raw = raw.get("projects") or []
        projects = []
        for proj in projects_raw:
            sources = []
            for src_idx, src in enumerate(proj.get("sources") or []):
                log_patterns = []
                log_pattern_names: set[str] = set()
                raw_log_patterns = src.get("log_patterns") or []

                # Validate max 50 log_patterns per source
                if len(raw_log_patterns) > 50:
                    raise ValueError(
                        f"Project '{proj.get('name', '')}' source index {src_idx}: "
                        f"log_patterns list exceeds maximum of 50 entries "
                        f"(got {len(raw_log_patterns)})"
                    )

                for lp_idx, lp in enumerate(raw_log_patterns):
                    lp_name = lp.get("name", "")
                    lp_regex = lp.get("regex", "")

                    # Validate duplicate name within same source
                    if lp_name in log_pattern_names:
                        raise ValueError(
                            f"Project '{proj.get('name', '')}' source index {src_idx}: "
                            f"duplicate log_patterns name '{lp_name}' at index {lp_idx}"
                        )
                    log_pattern_names.add(lp_name)

                    # Validate regex compilability
                    self._validate_regex(
                        lp_regex,
                        f"project '{proj.get('name', '')}' source index {src_idx} "
                        f"log_patterns[{lp_idx}] (name='{lp_name}')"
                    )

                    log_patterns.append(
                        LogPatternConfig(
                            name=lp_name,
                            regex=lp_regex,
                            event_type=lp.get("event_type", "unknown"),
                            priority=lp.get("priority", 500),
                        )
                    )
                sources.append(
                    SourcePattern(
                        pattern=src.get("pattern", ""),
                        type=src.get("type", "source"),
                        description=src.get("description", ""),
                        log_patterns=log_patterns,
                    )
                )

            # Parse log_settings at project level
            log_settings = None
            log_settings_raw = proj.get("log_settings")
            if log_settings_raw:
                log_settings = self._parse_log_settings(log_settings_raw)

            projects.append(
                ProjectConfig(
                    name=proj.get("name", ""),
                    description=proj.get("description", ""),
                    base_path=self.expand_path(proj.get("base_path", ".")),
                    sources=sources,
                    log_settings=log_settings,
                    auto_reindex=proj.get("auto_reindex", True),
                    removed=proj.get("removed", False),
                )
            )

        # Index extensions (discovery file-type list)
        raw_exts = raw.get("index_extensions")
        if raw_exts:
            index_extensions = []
            for e in raw_exts:
                ext = str(e.get("ext", "")).strip().lower()
                if ext and not ext.startswith("."):
                    ext = "." + ext
                if not ext:
                    continue
                index_extensions.append(
                    IndexExtension(
                        ext=ext,
                        type=e.get("type", "source"),
                        description=e.get("description", ""),
                    )
                )
        else:
            index_extensions = list(DEFAULT_INDEX_EXTENSIONS)

        return AppConfig(
            embedding=embedding,
            storage=storage,
            chunking=chunking,
            projects=projects,
            reranker=reranker,
            discovery_ignore=raw.get("discovery_ignore") or [],
            excluded_dirs=raw.get("excluded_dirs") or list(DEFAULT_EXCLUDED_DIRS),
            index_extensions=index_extensions,
        )

    def _parse_log_settings(self, raw: dict[str, Any]) -> LogSettings:
        """Parse log_settings from raw YAML dict."""
        from rag_mcp.log.parsing.config_models import (
            ContentTransformConfig,
            GroupingRuleConfig,
            LineFilterConfig,
        )

        line_filters = []
        for lf_idx, lf in enumerate(raw.get("line_filters", [])):
            lf_match = lf.get("match", "")
            lf_name = lf.get("name", "")

            # Validate regex compilability for line_filters[].match
            self._validate_regex(
                lf_match,
                f"log_settings.line_filters[{lf_idx}] (name='{lf_name}')"
            )

            line_filters.append(
                LineFilterConfig(
                    name=lf_name,
                    action=lf.get("action", "include"),
                    match=lf_match,
                    priority=lf.get("priority", 500),
                )
            )

        content_transforms = []
        for ct_idx, ct in enumerate(raw.get("content_transforms", [])):
            ct_match = ct.get("match", "")
            ct_name = ct.get("name", "")

            # Validate regex compilability for content_transforms[].match
            self._validate_regex(
                ct_match,
                f"log_settings.content_transforms[{ct_idx}] (name='{ct_name}')"
            )

            content_transforms.append(
                ContentTransformConfig(
                    name=ct_name,
                    match=ct_match,
                    action=ct.get("action", "extract"),
                    priority=ct.get("priority", 500),
                    fields=ct.get("fields"),
                    replacement=ct.get("replacement"),
                    max_length=ct.get("max_length"),
                    annotation_template=ct.get("annotation_template"),
                )
            )

        grouping_rules = []
        for gr_idx, gr in enumerate(raw.get("grouping_rules", [])):
            gr_name = gr.get("name", "")
            gr_start_pattern = gr.get("start_pattern", "")
            gr_continuation_patterns = gr.get("continuation_patterns", [])

            # Validate regex compilability for grouping_rules[].start_pattern
            self._validate_regex(
                gr_start_pattern,
                f"log_settings.grouping_rules[{gr_idx}] (name='{gr_name}') start_pattern"
            )

            # Validate regex compilability for each continuation_patterns[] entry
            for cp_idx, cp in enumerate(gr_continuation_patterns):
                self._validate_regex(
                    cp,
                    f"log_settings.grouping_rules[{gr_idx}] (name='{gr_name}') "
                    f"continuation_patterns[{cp_idx}]"
                )

            grouping_rules.append(
                GroupingRuleConfig(
                    name=gr_name,
                    start_pattern=gr_start_pattern,
                    continuation_patterns=gr_continuation_patterns,
                    time_window_ms=gr.get("time_window_ms"),
                )
            )

        return LogSettings(
            group_time_window_ms=raw.get("group_time_window_ms", 500),
            max_continuation_lines=raw.get("max_continuation_lines", 500),
            max_group_lines=raw.get("max_group_lines", 500),
            dedup_threshold=raw.get("dedup_threshold", 3),
            severity_mapping=raw.get("severity_mapping", {}),
            severity_types=raw.get("severity_types", []),
            line_filters=line_filters,
            content_transforms=content_transforms,
            grouping_rules=grouping_rules,
            default_filter_action=raw.get("default_filter_action", "include"),
        )

    def _serialize_config(self, config: AppConfig) -> dict[str, Any]:
        """Serialize AppConfig back to a dict for YAML output."""
        projects_data = []
        for proj in config.projects:
            sources_data = []
            for src in proj.sources:
                source_entry: dict[str, Any] = {
                    "pattern": src.pattern,
                    "type": src.type,
                }
                if src.description:
                    source_entry["description"] = src.description
                if src.log_patterns:
                    source_entry["log_patterns"] = [
                        {
                            "name": lp.name,
                            "regex": lp.regex,
                            "event_type": lp.event_type,
                            "priority": lp.priority,
                        }
                        for lp in src.log_patterns
                    ]
                sources_data.append(source_entry)

            proj_data: dict[str, Any] = {
                "name": proj.name,
                "description": proj.description,
                "base_path": proj.base_path,
                "sources": sources_data,
            }

            if not proj.auto_reindex:
                proj_data["auto_reindex"] = False

            if proj.removed:
                proj_data["removed"] = True

            if proj.log_settings:
                proj_data["log_settings"] = self._serialize_log_settings(
                    proj.log_settings
                )

            projects_data.append(proj_data)

        return {
            "embedding": {
                "model": config.embedding.model,
                "query_instruction": config.embedding.query_instruction,
            },
            "reranker": {
                "enabled": config.reranker.enabled,
                "model": config.reranker.model,
                "overfetch_factor": config.reranker.overfetch_factor,
            },
            "storage": {
                "path": config.storage.path,
                "collection_prefix": config.storage.collection_prefix,
                "mode": config.storage.mode,
                "url": config.storage.url,
            },
            "chunking": {
                "chunk_size": config.chunking.chunk_size,
                "chunk_overlap": config.chunking.chunk_overlap,
                "separators": config.chunking.separators,
            },
            "discovery_ignore": config.discovery_ignore,
            "excluded_dirs": config.excluded_dirs,
            "index_extensions": [
                {"ext": ie.ext, "type": ie.type, "description": ie.description}
                for ie in config.index_extensions
            ],
            "projects": projects_data,
        }

    def _serialize_log_settings(self, settings: LogSettings) -> dict[str, Any]:
        """Serialize LogSettings to a dict for YAML output."""
        data: dict[str, Any] = {
            "group_time_window_ms": settings.group_time_window_ms,
            "max_continuation_lines": settings.max_continuation_lines,
            "max_group_lines": settings.max_group_lines,
            "dedup_threshold": settings.dedup_threshold,
            "default_filter_action": settings.default_filter_action,
        }
        if settings.severity_mapping:
            data["severity_mapping"] = settings.severity_mapping
        if settings.severity_types:
            data["severity_types"] = settings.severity_types
        if settings.line_filters:
            data["line_filters"] = [
                {
                    "name": lf.name,
                    "action": lf.action,
                    "match": lf.match,
                    "priority": lf.priority,
                }
                for lf in settings.line_filters
            ]
        if settings.content_transforms:
            data["content_transforms"] = []
            for ct in settings.content_transforms:
                ct_data: dict[str, Any] = {
                    "name": ct.name,
                    "match": ct.match,
                    "action": ct.action,
                    "priority": ct.priority,
                }
                if ct.fields is not None:
                    ct_data["fields"] = ct.fields
                if ct.replacement is not None:
                    ct_data["replacement"] = ct.replacement
                if ct.max_length is not None:
                    ct_data["max_length"] = ct.max_length
                if ct.annotation_template is not None:
                    ct_data["annotation_template"] = ct.annotation_template
                data["content_transforms"].append(ct_data)
        if settings.grouping_rules:
            data["grouping_rules"] = [
                {
                    "name": gr.name,
                    "start_pattern": gr.start_pattern,
                    "continuation_patterns": gr.continuation_patterns,
                    **({"time_window_ms": gr.time_window_ms} if gr.time_window_ms is not None else {}),
                }
                for gr in settings.grouping_rules
            ]
        return data
