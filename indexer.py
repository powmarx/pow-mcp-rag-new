"""
Document Indexer for the RAG MCP Server.

Reads project files defined in config.yaml, chunks them, generates embeddings,
and stores them in a local ChromaDB instance.

Usage:
    python indexer.py                          # Index all projects
    python indexer.py --project NAME           # Index a specific project
    python indexer.py --reset                  # Clear and re-index everything
    python indexer.py --prune                  # Remove chunks for deleted files
    python indexer.py --add-project --name NAME --path PATH        # Add a new project
    python indexer.py --add-folder --project NAME --path DIR [--pattern GLOB]  # Add a folder to an existing project
    python indexer.py --add-pattern --project NAME --pattern GLOB [--type TYPE] [--description DESC]  # Add a pattern to an existing project
"""

import argparse
import os
import sys
from pathlib import Path

# Add src/ to path so rag_mcp package is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

from rich.console import Console

from rag_mcp.auto_detector import ProjectAutoDetector
from rag_mcp.chroma_store import ChromaStore
from rag_mcp.chunker import Chunker
from rag_mcp.config_loader import ConfigLoader, ProjectConfig, SourcePattern
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.file_reader import FileReader
from rag_mcp.indexing_pipeline import IndexingPipeline
from rag_mcp.pdf_converter import PDFConverter

# Config path: RAG_CONFIG_PATH env override (used by Docker to read the config
# from the data volume), else config.yaml next to this script.
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("RAG_CONFIG_PATH") or (SCRIPT_DIR / "config" / "config.yaml"))

console = Console(highlight=False, force_terminal=False)


def main():
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

    # Handle --add-project separately
    if args.add_project:
        return handle_add_project(args)

    # Handle --add-folder separately
    if args.add_folder:
        return handle_add_folder(args)

    # Handle --add-pattern separately
    if args.add_pattern:
        return handle_add_pattern(args)

    # Handle --convert-pdfs separately
    if args.convert_pdfs:
        return handle_convert_pdfs(args)

    # Handle --estimate separately
    if args.estimate:
        return handle_estimate(args)

    # Normal indexing flow
    console.print("[bold green]RAG Document Indexer[/bold green]")
    console.print(f"Config: {CONFIG_PATH}\n")

    # Load configuration
    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        sys.exit(1)

    # Load embedding model
    embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
    console.print("Loading embedding model...")
    try:
        embedding_gen.load()
    except Exception as e:
        console.print(f"[red]Error loading embedding model: {e}[/red]")
        sys.exit(1)

    console.print(f"[green]Model loaded:[/green] {config.embedding.model}\n")

    # Connect to ChromaDB
    store = ChromaStore(config.storage)
    store.connect()

    # Create pipeline components
    file_reader = FileReader()
    chunker = Chunker(config.chunking)
    pipeline = IndexingPipeline(
        config=config,
        file_reader=file_reader,
        chunker=chunker,
        embedding_gen=embedding_gen,
        store=store,
        console=console,
    )

    # Filter projects if --project specified
    projects = config.projects
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            console.print(f"[red]Project '{args.project}' not found in config.yaml[/red]")
            console.print("Available projects:")
            for p in config.projects:
                console.print(f"  - {p.name}")
            sys.exit(1)

    # Run indexing
    total_chunks = 0
    for project in projects:
        if args.prune:
            pipeline.prune_project(project)

        chunks = pipeline.index_project(project, reset=args.reset)
        total_chunks += chunks

    console.print(f"\n[bold green]Indexing complete! Total: {total_chunks} chunks[/bold green]")


def handle_add_project(args):
    """Handle the --add-project command."""
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

    # Run auto-detection
    detector = ProjectAutoDetector()
    detected = detector.detect(project_path)

    if not detected:
        console.print("[yellow]No recognizable patterns detected. Adding empty project entry.[/yellow]")
        console.print("Edit config.yaml manually to add source patterns.\n")

    # Build project config
    sources = [
        SourcePattern(pattern=d.pattern, type=d.type, description=d.description)
        for d in detected
    ]
    new_project = ProjectConfig(
        name=args.name,
        description=f"Auto-detected project at {project_path.name}",
        base_path=str(project_path).replace("\\", "/"),
        sources=sources,
    )

    # Display detected configuration
    console.print("[cyan]Detected source patterns:[/cyan]")
    for src in sources:
        console.print(f"  [{src.type}] {src.pattern} — {src.description}")

    # Load existing config and append
    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except FileNotFoundError:
        console.print(f"[red]Error: Config file not found: {CONFIG_PATH}[/red]")
        sys.exit(1)

    # Check for duplicate name
    existing_names = {p.name for p in config.projects}
    if args.name in existing_names:
        console.print(f"\n[yellow]Warning: Project '{args.name}' already exists in config.yaml[/yellow]")
        console.print("The existing entry will be replaced.")
        config.projects = [p for p in config.projects if p.name != args.name]

    config.projects.append(new_project)
    loader.save(config)

    console.print(f"\n[green]Project '{args.name}' added to config.yaml[/green]")
    console.print("Run [cyan]python indexer.py --project {name}[/cyan] to index it.".format(name=args.name))


def handle_add_folder(args):
    """Handle the --add-folder command. Adds a folder pattern to an existing
    project and indexes matching files immediately (mirrors the add_folder MCP tool)."""
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

    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error loading config: {e}[/red]")
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

    import glob as glob_mod
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

        from rag_mcp.tools.helpers import FILE_TYPE_MAP
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


def handle_add_pattern(args):
    """Handle the --add-pattern command. Adds a glob pattern to an existing
    project and indexes matching files immediately (mirrors the add_pattern MCP tool)."""
    if not args.project:
        console.print("[red]Error: --project is required with --add-pattern (target project name)[/red]")
        sys.exit(1)
    if not args.pattern or args.pattern == "**/*":
        console.print("[red]Error: --pattern is required with --add-pattern (e.g. 'doc/specs/**/*.md')[/red]")
        sys.exit(1)

    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error loading config: {e}[/red]")
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
    import glob as glob_mod
    full_pattern = str(base_path / args.pattern)
    matched = glob_mod.glob(full_pattern, recursive=True)
    files = [Path(f) for f in matched if Path(f).is_file()]

    desc = args.description or f"Manually added pattern: {args.pattern}"
    existing_patterns = {s.pattern for s in project_config.sources}
    pattern_is_new = args.pattern not in existing_patterns

    if not files:
        # No files matched yet — persist the pattern anyway (picked up on future indexer runs)
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

    from rag_mcp.tools.helpers import FILE_TYPE_MAP
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


def handle_estimate(args):
    """Handle the --estimate command. Scans files and estimates chunks/DB size without indexing."""
    console.print("[bold green]RAG Index Estimate[/bold green]")
    console.print(f"Config: {CONFIG_PATH}\n")

    # Load configuration
    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        sys.exit(1)

    from rag_mcp.file_reader import FileReader
    from rag_mcp.chunker import Chunker
    import glob as glob_mod

    # Convert PDFs into the writable cache (works on read-only source mounts).
    file_reader = FileReader(pdf_cache_dir=Path(config.storage.path) / "pdf_cache")
    chunker = Chunker(config.chunking)

    # Filter projects if --project specified
    projects = config.projects
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            console.print(f"[red]Project '{args.project}' not found[/red]")
            sys.exit(1)

    KB_PER_CHUNK = 10.0  # empirical: ~10 KB per chunk on disk (including indexes)
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
            files = glob_mod.glob(pattern, recursive=True)
            files = [f for f in files if Path(f).is_file()]

            if not files:
                continue

            # Estimate chunks per file by reading and chunking (no embeddings)
            source_chunks = 0
            for filepath_str in files:
                filepath = Path(filepath_str)
                content = file_reader.read(filepath, base_path)
                if content is None:
                    continue
                chunks = chunker.chunk(content.content)
                source_chunks += len(chunks)

            project_files += len(files)
            project_chunks += source_chunks
            console.print(
                f"  {source.pattern}: {len(files)} files -> ~{source_chunks} chunks"
            )

        console.print(
            f"  [cyan]Subtotal: {project_files} files, ~{project_chunks} chunks, "
            f"~{project_chunks * KB_PER_CHUNK / 1024:.1f} MB on disk[/cyan]"
        )
        console.print()

        grand_total_files += project_files
        grand_total_chunks += project_chunks

    estimated_mb = grand_total_chunks * KB_PER_CHUNK / 1024
    console.print(f"[bold green]Total estimate:[/bold green]")
    console.print(f"  Files: {grand_total_files}")
    console.print(f"  Chunks: ~{grand_total_chunks}")
    console.print(f"  DB size: ~{estimated_mb:.0f} MB")
    console.print(f"\n  (Based on ~{KB_PER_CHUNK:.0f} KB per chunk including ChromaDB indexes)")


def handle_convert_pdfs(args):
    """Handle the --convert-pdfs command."""
    # Load config to find project paths
    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        sys.exit(1)

    converter = PDFConverter()

    if args.path:
        # Convert PDFs in a specific directory
        target_path = Path(args.path).resolve()
        if not target_path.exists():
            console.print(f"[red]Error: Path does not exist: {target_path}[/red]")
            sys.exit(1)
        console.print(f"[bold green]Converting PDFs in: {target_path}[/bold green]")
        converted = converter.convert_directory(target_path)
        console.print(f"[green]Converted {len(converted)} PDF(s) to Markdown[/green]")
    elif args.project:
        # Convert PDFs for a specific project
        projects = [p for p in config.projects if p.name == args.project]
        if not projects:
            console.print(f"[red]Project '{args.project}' not found[/red]")
            sys.exit(1)
        project = projects[0]
        base_path = Path(project.base_path)
        console.print(f"[bold green]Converting PDFs for: {project.name}[/bold green]")
        console.print(f"  Path: {base_path}")

        # Find PDF patterns in project config
        import glob as glob_mod
        total_converted = 0
        for source in project.sources:
            if "*.pdf" in source.pattern:
                pattern = str(base_path / source.pattern)
                pdf_files = glob_mod.glob(pattern, recursive=True)
                for pdf_file in pdf_files:
                    result = converter.convert(Path(pdf_file))
                    if result:
                        console.print(f"  [green]{Path(pdf_file).name} -> {result.name}[/green]")
                        total_converted += 1
        console.print(f"\n[green]Converted {total_converted} PDF(s) to Markdown[/green]")
    else:
        # Convert PDFs for all projects
        console.print("[bold green]Converting PDFs for all projects[/bold green]")
        total_converted = 0
        import glob as glob_mod
        for project in config.projects:
            base_path = Path(project.base_path)
            if not base_path.exists():
                continue
            for source in project.sources:
                if "*.pdf" in source.pattern:
                    pattern = str(base_path / source.pattern)
                    pdf_files = glob_mod.glob(pattern, recursive=True)
                    for pdf_file in pdf_files:
                        result = converter.convert(Path(pdf_file))
                        if result:
                            console.print(f"  [green]{Path(pdf_file).name} -> {result.name}[/green]")
                            total_converted += 1
        console.print(f"\n[green]Converted {total_converted} PDF(s) to Markdown[/green]")


if __name__ == "__main__":
    main()
