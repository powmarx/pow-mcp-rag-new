"""
PDF to Markdown converter.

Converts PDF files to structured Markdown for better chunking and indexing.
Outputs .md files alongside the original PDFs.
"""

import sys
from pathlib import Path

import fitz  # PyMuPDF


class PDFConverter:
    """Converts PDF files to Markdown format."""

    def convert(self, pdf_path: Path, output_path: Path | None = None) -> Path | None:
        """
        Convert a PDF file to Markdown.

        Args:
            pdf_path: Path to the PDF file.
            output_path: Optional output path. Defaults to same directory with .md extension.

        Returns:
            Path to the generated .md file, or None on failure.
        """
        if output_path is None:
            output_path = pdf_path.with_suffix(".md")

        try:
            doc = fitz.open(str(pdf_path))
        except Exception as e:
            print(f"  [error] Failed to open PDF: {pdf_path}: {e}", file=sys.stderr)
            return None

        md_parts = []
        md_parts.append(f"# {pdf_path.stem}\n")
        md_parts.append(f"> Auto-converted from: `{pdf_path.name}`\n")

        try:
            for page_num, page in enumerate(doc, 1):
                text = page.get_text("text")
                if not text or not text.strip():
                    continue

                # Add page separator as heading
                md_parts.append(f"\n## Page {page_num}\n")

                # Process text: clean up common PDF artifacts
                cleaned = self._clean_page_text(text)
                md_parts.append(cleaned)
        finally:
            doc.close()

        if len(md_parts) <= 2:
            # Only header and source note, no actual content
            print(
                f"  [warning] PDF has no extractable text: {pdf_path.name}",
                file=sys.stderr,
            )
            return None

        # Write markdown file
        content = "\n".join(md_parts)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"  [error] Failed to write markdown: {output_path}: {e}", file=sys.stderr)
            return None

        return output_path

    def convert_directory(self, directory: Path, recursive: bool = True) -> list[Path]:
        """
        Convert all PDFs in a directory to Markdown.

        Args:
            directory: Directory to scan for PDFs.
            recursive: Whether to scan subdirectories.

        Returns:
            List of generated .md file paths.
        """
        pattern = "**/*.pdf" if recursive else "*.pdf"
        converted = []

        for pdf_path in directory.glob(pattern):
            md_path = pdf_path.with_suffix(".md")

            # Skip if markdown already exists and is newer than PDF
            if md_path.exists() and md_path.stat().st_mtime >= pdf_path.stat().st_mtime:
                print(f"  [skip] Already converted: {pdf_path.name}", file=sys.stderr)
                converted.append(md_path)
                continue

            result = self.convert(pdf_path)
            if result:
                converted.append(result)
                print(f"  [converted] {pdf_path.name} -> {result.name}", file=sys.stderr)

        return converted

    def _clean_page_text(self, text: str) -> str:
        """Clean common PDF extraction artifacts from page text."""
        lines = text.split("\n")
        cleaned_lines = []

        for line in lines:
            stripped = line.strip()

            # Skip empty lines (but keep paragraph breaks)
            if not stripped:
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
                continue

            # Skip page number lines (common pattern: just a number)
            if stripped.isdigit() and len(stripped) <= 4:
                continue

            # Detect potential headings (short lines, often uppercase or title case)
            if (
                len(stripped) < 80
                and not stripped.endswith(".")
                and not stripped.endswith(",")
                and stripped[0].isupper()
                and stripped.count(" ") < 10
            ):
                # Could be a section heading - make it bold
                cleaned_lines.append(f"**{stripped}**")
            else:
                cleaned_lines.append(stripped)

        return "\n".join(cleaned_lines)
