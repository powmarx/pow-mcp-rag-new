"""
Importable entry point for the indexer.

Used by the CLI (rag-mcp-new-pip-mcp index) so the indexer can be run
both as a standalone script (python indexer.py) and as an installed package.

When running as a package, RAG_CONFIG_PATH is already set by cli.py.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Run the indexer. Delegates to indexer.py logic."""
    here = Path(__file__).parent
    src_dir = here.parent
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    candidates = [
        here.parent.parent / "indexer.py",
        Path(sys.prefix) / "lib" / "rag_mcp" / "indexer.py",
    ]

    indexer_py = next((p for p in candidates if p.exists()), None)

    if indexer_py:
        import runpy
        runpy.run_path(str(indexer_py), run_name="__main__")
    else:
        _run_inline()


def _run_inline() -> None:
    """Inline indexer startup — used when indexer.py is not found on disk
    (real pip/uvx/uv-tool-install packages). Mirrors indexer.py's CLI
    surface (--add-project, --add-folder, --add-pattern, --convert-pdfs,
    --estimate, --prune) so behavior doesn't depend on install mode.
    """
    import argparse
    import os
    from pathlib import Path

    from rag_mcp.paths import resolve_config_path, resolve_data_path
    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.config_loader import ConfigLoader
    from rag_mcp.embedding_generator import EmbeddingGenerator
    from rag_mcp.file_reader import FileReader
    from rag_mcp.chunker import Chunker
    from rag_mcp.indexing_pipeline import IndexingPipeline
    from rich.console import Console

    config_path = Path(os.environ.get("RAG_CONFIG_PATH", ""))
    if not config_path.exists():
        print(f"[fatal] Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    console = Console(highlight=False)

    parser = argparse.ArgumentParser(description="Index project documents for RAG")
    parser.add_argument("--project", type=str, help="Index only a specific project by name (or target project for --add-folder/--add-pattern)")
    parser.add_argument("--reset", action="store_true", help="Clear and re-index everything")
    parser.add_argument("--prune", action="store_true", help="Remove chunks for deleted files")
    parser.add_argument("--add-project", action="store_true", help="Add a new project to config")
    parser.add_argument("--add-folder", action="store_true", help="Add a folder (with --path) to an existing project (--project)")
    parser.add_argument("--add-pattern", action="store_true", help="Add a glob pattern to an existing project (--project)")
    parser.add_argument("--convert-pdfs", action="store_true", help="Convert PDFs to Markdown before indexing")
    parser.add_argument("--estimate", action="store_true", help="Estimate chunks and DB size without indexing")
    parser.add_argument("--name", type=str, help="Project name (for --add-project)")
    parser.add_argument("--path", type=str, help="Project/folder path (for --add-project, --add-folder, or --convert-pdfs)")
    parser.add_argument("--pattern", type=str, default="**/*", help="Glob pattern (for --add-folder, relative to --path; or --add-pattern, relative to project base_path)")
    parser.add_argument("--type", type=str, default="documentation", choices=["source", "header", "documentation", "config"], help="File type classification (for --add-pattern; default: documentation)")
    parser.add_argument("--description", type=str, default="", help="Description for the new source entry (for --add-pattern)")
    args = parser.parse_args()

    loader = ConfigLoader(config_path)
    config = loader.load()

    data_path = resolve_data_path()
    if not Path(config.storage.path).is_absolute():
        config.storage.path = str(data_path)
        data_path.mkdir(parents=True, exist_ok=True)

    # --- --add-project: just registers the project in config, no indexing ---
    if args.add_project:
        _handle_add_project(args, config, loader, console)
        return

    # --- --add-folder / --add-pattern: index immediately + persist pattern ---
    if args.add_folder:
        _handle_add_folder(args, config, loader, console)
        return
    if args.add_pattern:
        _handle_add_pattern(args, config, loader, console)
        return

    store = ChromaStore(config.storage)
    store.connect()

    # --- --convert-pdfs: standalone conversion, no embeddings needed ---
    if args.convert_pdfs:
        _handle_convert_pdfs(args, config, console)
        return

    # --- --estimate: dry run, no embeddings/writes ---
    if args.estimate:
        _handle_estimate(args, config, console)
        return

    embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
    embedding_gen.load()

    file_reader = FileReader(pdf_cache_dir=Path(config.storage.path) / "pdf_cache")
    chunker = Chunker(config.chunking)

    pipeline = IndexingPipeline(
        config=config,
        file_reader=file_reader,
        chunker=chunker,
        embedding_gen=embedding_gen,
        store=store,
        console=console,
    )

    projects = (
        [p for p in config.projects if p.name == args.project and not p.removed]
        if args.project else
        [p for p in config.projects if not p.removed]
    )

    if args.project and not projects:
        console.print(f"[red]Project '{args.project}' not found in config.yaml[/red]")
        console.print("Available projects:")
        for p in config.projects:
            console.print(f"  - {p.name}")
        sys.exit(1)

    total_chunks = 0
    for project in projects:
        if args.prune:
            pipeline.prune_project(project)
        chunks = pipeline.index_project(project, reset=args.reset)
        total_chunks += chunks
        console.print(f"[green]Done[/green] {project.name}: {chunks} chunks")

    console.print(f"\n[bold green]Indexing complete! Total: {total_chunks} chunks[/bold green]")


def _handle_add_project(args, config, loader, console) -> None:
    """Auto-detect a project's structure and register it in config.yaml
    (does not index — mirrors indexer.py's handle_add_project)."""
    from rag_mcp.auto_detector import ProjectAutoDetector
    from rag_mcp.config_loader import ProjectConfig, SourcePattern

    if not args.name:
        console.print("[red]Error: --name is required with --add-project[/red]")
        sys.exit(1)

    project_path = Path(args.path) if args.path else Path.cwd()
    project_path = project_path.resolve()
    if not project_path.exists():
        console.print(f"[red]Error: Path does not exist: {project_path}[/red]")
        sys.exit(1)

    console.print(f"[bold green]Auto-detecting project structure[/bold green]")
    console.print(f"  Name: {args.name}")
    console.print(f"  Path: {project_path}\n")

    detector = ProjectAutoDetector()
    detected = detector.detect(project_path)
    if not detected:
        console.print("[yellow]No recognizable patterns detected. Adding empty project entry.[/yellow]")

    sources = [SourcePattern(pattern=d.pattern, type=d.type, description=d.description) for d in detected]
    new_project = ProjectConfig(
        name=args.name,
        description=f"Auto-detected project at {project_path.name}",
        base_path=str(project_path).replace("\\", "/"),
        sources=sources,
    )

    console.print("[cyan]Detected source patterns:[/cyan]")
    for src in sources:
        console.print(f"  [{src.type}] {src.pattern} — {src.description}")

    existing_names = {p.name for p in config.projects}
    if args.name in existing_names:
        console.print(f"\n[yellow]Warning: Project '{args.name}' already exists in config.yaml[/yellow]")
        console.print("The existing entry will be replaced.")
        config.projects = [p for p in config.projects if p.name != args.name]

    config.projects.append(new_project)
    loader.save(config)

    console.print(f"\n[green]Project '{args.name}' added to config.yaml[/green]")
    console.print(f"Run [cyan]rag-mcp index --project {args.name}[/cyan] to index it.")


def _handle_add_folder(args, config, loader, console) -> None:
    """Index a folder into an existing project and persist the pattern
    (mirrors indexer.py's handle_add_folder / the add_folder MCP tool)."""
    import glob as glob_mod
    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import SourcePattern
    from rag_mcp.embedding_generator import EmbeddingGenerator
    from rag_mcp.file_reader import FileReader
    from rag_mcp.tools.helpers import FILE_TYPE_MAP

    if not args.project:
        console.print("[red]Error: --project is required with --add-folder (target project name)[/red]")
        sys.exit(1)
    if not args.path:
        console.print("[red]Error: --path is required with --add-folder (folder to add)[/red]")
        sys.exit(1)

    target_dir = Path(args.path).resolve()
    if not target_dir.exists():
        console.print(f"[red]Error: Folder does not exist: {target_dir}[/red]")
        sys.exit(1)
    if not target_dir.is_dir():
        console.print(f"[red]Error: Path is not a folder: {target_dir}[/red]")
        sys.exit(1)

    project_config = next((p for p in config.projects if p.name == args.project), None)
    if project_config is None:
        console.print(f"[red]Error: Project '{args.project}' not found in config.yaml[/red]")
        console.print("Available projects:")
        for p in config.projects:
            console.print(f"  - {p.name}")
        sys.exit(1)

    base_path = Path(project_config.base_path)
    try:
        relative_dir = target_dir.relative_to(base_path)
        relative_dir_str = str(relative_dir).replace("\\", "/")
    except ValueError:
        console.print(f"[red]Error: Folder must be inside the project base_path ({base_path})[/red]")
        sys.exit(1)

    full_pattern = str(target_dir / args.pattern)
    files = [f for f in glob_mod.glob(full_pattern, recursive=True) if Path(f).is_file()]
    if not files:
        console.print(f"[red]Error: No files found matching pattern '{args.pattern}' in {target_dir}[/red]")
        sys.exit(1)

    embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
    console.print("Loading embedding model...")
    embedding_gen.load()

    store = ChromaStore(config.storage)
    store.connect()
    collection = store.get_collection(args.project)
    if collection is None:
        console.print(f"[red]Error: Project '{args.project}' has no index. Run the indexer for it first.[/red]")
        sys.exit(1)

    file_reader = FileReader(pdf_cache_dir=Path(config.storage.path) / "pdf_cache")
    chunker = Chunker(config.chunking)

    total_chunks = 0
    indexed_files = 0
    for filepath_str in files:
        filepath = Path(filepath_str)
        content = file_reader.read(filepath, base_path)
        if content is None:
            continue
        chunks = chunker.chunk(content.content)
        if not chunks:
            continue
        chunk_texts = [c.content for c in chunks]
        embeddings = embedding_gen.encode(chunk_texts)
        file_type = FILE_TYPE_MAP.get(filepath.suffix.lower(), "source")

        store.delete_file_chunks(collection, content.relative_path)
        metadata_base = {
            "file_path": content.relative_path,
            "file_type": file_type,
            "file_hash": content.file_hash,
            "project": args.project,
            "source_description": f"Manually added folder: {relative_dir_str}",
        }
        store.upsert_chunks(collection, content.relative_path, chunk_texts, embeddings, metadata_base)
        total_chunks += len(chunks)
        indexed_files += 1

    config_pattern = f"{relative_dir_str}/{args.pattern}"
    existing_patterns = {s.pattern for s in project_config.sources}
    if config_pattern not in existing_patterns:
        project_config.sources.append(SourcePattern(
            pattern=config_pattern,
            type="source",
            description=f"Manually added folder: {relative_dir_str}",
        ))
        loader.save(config)

    console.print(f"\n[green]Folder indexed and saved to config![/green]")
    console.print(f"  Folder: {relative_dir_str}")
    console.print(f"  Pattern: {config_pattern}")
    console.print(f"  Project: {args.project}")
    console.print(f"  Files indexed: {indexed_files}")
    console.print(f"  Chunks: {total_chunks}")


def _handle_add_pattern(args, config, loader, console) -> None:
    """Add a glob pattern to an existing project and index matching files
    (mirrors indexer.py's handle_add_pattern / the add_pattern MCP tool)."""
    import glob as glob_mod
    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import SourcePattern
    from rag_mcp.embedding_generator import EmbeddingGenerator
    from rag_mcp.file_reader import FileReader
    from rag_mcp.tools.helpers import FILE_TYPE_MAP

    if not args.project:
        console.print("[red]Error: --project is required with --add-pattern (target project name)[/red]")
        sys.exit(1)
    if not args.pattern or args.pattern == "**/*":
        console.print("[red]Error: --pattern is required with --add-pattern (e.g. 'doc/specs/**/*.md')[/red]")
        sys.exit(1)

    project_config = next((p for p in config.projects if p.name == args.project), None)
    if project_config is None:
        console.print(f"[red]Error: Project '{args.project}' not found in config.yaml[/red]")
        console.print("Available projects:")
        for p in config.projects:
            console.print(f"  - {p.name}")
        sys.exit(1)
    if project_config.removed:
        console.print(f"[red]Error: Project '{args.project}' is marked as removed. Re-add it first with --add-project.[/red]")
        sys.exit(1)

    base_path = Path(project_config.base_path)
    full_pattern = str(base_path / args.pattern)
    matched = glob_mod.glob(full_pattern, recursive=True)
    files = [Path(f) for f in matched if Path(f).is_file()]

    desc = args.description or f"Manually added pattern: {args.pattern}"
    existing_patterns = {s.pattern for s in project_config.sources}
    pattern_is_new = args.pattern not in existing_patterns

    if not files:
        if pattern_is_new:
            project_config.sources.append(SourcePattern(pattern=args.pattern, type=args.type, description=desc))
            loader.save(config)
        console.print(f"\n[yellow]Pattern saved to config (no files matched yet).[/yellow]")
        console.print(f"  Project: {args.project}")
        console.print(f"  Pattern: {args.pattern}")
        console.print(f"  Type: {args.type}")
        console.print(f"  Note: No files matched '{full_pattern}' right now.")
        return

    embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
    console.print("Loading embedding model...")
    embedding_gen.load()

    store = ChromaStore(config.storage)
    store.connect()
    collection = store.get_collection(args.project)
    if collection is None:
        console.print(f"[red]Error: Project '{args.project}' has no index. Run the indexer for it first.[/red]")
        sys.exit(1)

    file_reader = FileReader(pdf_cache_dir=Path(config.storage.path) / "pdf_cache")
    chunker = Chunker(config.chunking)

    total_chunks = 0
    indexed_files = 0
    for filepath in files:
        content = file_reader.read(filepath, base_path)
        if content is None:
            continue
        chunks = chunker.chunk(content.content)
        if not chunks:
            continue
        chunk_texts = [c.content for c in chunks]
        embeddings = embedding_gen.encode(chunk_texts)
        file_type = FILE_TYPE_MAP.get(filepath.suffix.lower(), args.type)

        store.delete_file_chunks(collection, content.relative_path)
        metadata_base = {
            "file_path": content.relative_path,
            "file_type": file_type,
            "file_hash": content.file_hash,
            "project": args.project,
            "source_description": desc,
        }
        store.upsert_chunks(collection, content.relative_path, chunk_texts, embeddings, metadata_base)
        total_chunks += len(chunks)
        indexed_files += 1

    if pattern_is_new:
        project_config.sources.append(SourcePattern(pattern=args.pattern, type=args.type, description=desc))
        loader.save(config)

    console.print(f"\n[green]Pattern indexed and saved to config![/green]")
    console.print(f"  Project: {args.project}")
    console.print(f"  Pattern: {args.pattern}")
    console.print(f"  Type: {args.type}")
    console.print(f"  Files indexed: {indexed_files}")
    console.print(f"  Chunks: {total_chunks}")
    console.print(f"  Config: {'added (new)' if pattern_is_new else 'already present (updated index)'}")


def _handle_convert_pdfs(args, config, console) -> None:
    """Convert PDF sources to Markdown (mirrors indexer.py's handle_convert_pdfs)."""
    import glob as glob_mod
    from rag_mcp.pdf_converter import PDFConverter

    converter = PDFConverter()

    if args.path:
        target_path = Path(args.path).resolve()
        if not target_path.exists():
            console.print(f"[red]Error: Path does not exist: {target_path}[/red]")
            sys.exit(1)
        console.print(f"[bold green]Converting PDFs in: {target_path}[/bold green]")
        converted = converter.convert_directory(target_path)
        console.print(f"[green]Converted {len(converted)} PDF(s) to Markdown[/green]")
        return

    if args.project:
        projects = [p for p in config.projects if p.name == args.project]
        if not projects:
            console.print(f"[red]Project '{args.project}' not found[/red]")
            sys.exit(1)
        target_projects = projects
    else:
        target_projects = config.projects

    total_converted = 0
    for project in target_projects:
        base_path = Path(project.base_path)
        if not base_path.exists():
            continue
        console.print(f"[bold green]Converting PDFs for: {project.name}[/bold green]")
        for source in project.sources:
            if "*.pdf" not in source.pattern:
                continue
            pattern = str(base_path / source.pattern)
            for pdf_file in glob_mod.glob(pattern, recursive=True):
                result = converter.convert(Path(pdf_file))
                if result:
                    console.print(f"  [green]{Path(pdf_file).name} -> {result.name}[/green]")
                    total_converted += 1

    console.print(f"\n[green]Converted {total_converted} PDF(s) to Markdown[/green]")


def _handle_estimate(args, config, console) -> None:
    """Dry-run chunk/size estimate (mirrors indexer.py's handle_estimate)."""
    import glob as glob_mod
    from rag_mcp.chunker import Chunker
    from rag_mcp.file_reader import FileReader

    console.print("[bold green]RAG Index Estimate[/bold green]\n")

    file_reader = FileReader(pdf_cache_dir=Path(config.storage.path) / "pdf_cache")
    chunker = Chunker(config.chunking)

    projects = config.projects
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            console.print(f"[red]Project '{args.project}' not found[/red]")
            sys.exit(1)

    KB_PER_CHUNK = 10.0
    grand_total_files = 0
    grand_total_chunks = 0

    for project in projects:
        base_path = Path(project.base_path)
        if not base_path.exists():
            console.print(f"  [yellow]{project.name}: path not found, skipping[/yellow]")
            continue

        project_files = 0
        project_chunks = 0
        console.print(f"[bold blue]{project.name}[/bold blue]")
        console.print(f"  Path: {base_path}")

        for source in project.sources:
            pattern = str(base_path / source.pattern)
            files = [f for f in glob_mod.glob(pattern, recursive=True) if Path(f).is_file()]
            if not files:
                continue

            source_chunks = 0
            for filepath_str in files:
                content = file_reader.read(Path(filepath_str), base_path)
                if content is None:
                    continue
                source_chunks += len(chunker.chunk(content.content))

            project_files += len(files)
            project_chunks += source_chunks
            console.print(f"  {source.pattern}: {len(files)} files -> ~{source_chunks} chunks")

        console.print(
            f"  [cyan]Subtotal: {project_files} files, ~{project_chunks} chunks, "
            f"~{project_chunks * KB_PER_CHUNK / 1024:.1f} MB on disk[/cyan]\n"
        )
        grand_total_files += project_files
        grand_total_chunks += project_chunks

    estimated_mb = grand_total_chunks * KB_PER_CHUNK / 1024
    console.print(f"[bold green]Total estimate:[/bold green]")
    console.print(f"  Files: {grand_total_files}")
    console.print(f"  Chunks: ~{grand_total_chunks}")
    console.print(f"  DB size: ~{estimated_mb:.0f} MB")
