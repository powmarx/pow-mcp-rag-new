"""
PDF text extraction using PyMuPDF (pymupdf).

Extracts text content from PDF files for indexing into the RAG system.
Handles multi-page documents, mixed text/image pages, and image-only PDFs.
"""

import sys
from pathlib import Path

import pymupdf  # PyMuPDF


class PDFExtractor:
    """Extracts text content from PDF files."""

    def extract(self, filepath: Path) -> str | None:
        """
        Extract all text from a PDF file.

        Returns concatenated page text, or None if no extractable text found.
        """
        try:
            doc = pymupdf.open(str(filepath))
        except Exception as e:
            print(f"  [warning] Failed to open PDF {filepath}: {e}", file=sys.stderr)
            return None

        pages_text = []
        try:
            for page in doc:
                text = page.get_text("text")
                if text and text.strip():
                    pages_text.append(text.strip())
        finally:
            doc.close()

        if not pages_text:
            return None

        return "\n\n".join(pages_text)

    def has_text(self, filepath: Path) -> bool:
        """Quick check if PDF contains any extractable text."""
        try:
            doc = pymupdf.open(str(filepath))
            try:
                for page in doc:
                    text = page.get_text("text")
                    if text and text.strip():
                        return True
                return False
            finally:
                doc.close()
        except Exception:
            return False
