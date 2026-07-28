"""
Markdown to HTML/PDF converter using markdown-it-py.

Generates a self-contained HTML file with print-optimized CSS.
Then attempts PDF conversion via available tools (wkhtmltopdf, chrome headless).

Usage:
    python md_to_pdf.py <input.md> [output.pdf]
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from markdown_it import MarkdownIt


CSS = """
@page {
    size: A4 landscape;
    margin: 10mm 12mm;
}
* { box-sizing: border-box; }
body {
    font-family: -apple-system, "Segoe UI", Arial, sans-serif;
    font-size: 9pt;
    line-height: 1.4;
    color: #1a1a1a;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 16pt;
    border-bottom: 2px solid #333;
    padding-bottom: 4pt;
    margin: 0 0 8pt 0;
    width: 100%;
}
h2 {
    font-size: 12pt;
    color: #222;
    margin: 14pt 0 6pt 0;
    border-bottom: 1px solid #ccc;
    padding-bottom: 2pt;
    width: 100%;
}
h3 {
    font-size: 10pt;
    color: #333;
    margin: 10pt 0 4pt 0;
    width: 100%;
}
p, li, blockquote {
    width: 100%;
}
table {
    border-collapse: collapse;
    font-size: 8pt;
    margin: 6pt 0 10pt 0;
    page-break-inside: avoid;
}
th, td {
    border: 1px solid #ddd;
    padding: 5pt 10pt;
    text-align: left;
    vertical-align: top;
    white-space: nowrap;
}
th {
    background-color: #f6f8fa;
    font-weight: 600;
}
td:last-child, th:last-child {
    white-space: normal;
}
pre {
    font-family: "Consolas", "Courier New", monospace;
    font-size: 5.8pt;
    background-color: #f6f6f6;
    border: 1px solid #ddd;
    border-radius: 3px;
    padding: 6pt;
    line-height: 1.3;
    white-space: pre-wrap;
    word-wrap: break-word;
    page-break-inside: avoid;
    overflow-x: hidden;
}
code {
    font-family: "Consolas", "Courier New", monospace;
    font-size: 7.5pt;
    background-color: #f0f0f0;
    padding: 1pt 3pt;
    border-radius: 2px;
}
pre code {
    background: none;
    padding: 0;
    font-size: inherit;
}
blockquote {
    border-left: 3px solid #bbb;
    margin: 6pt 0 6pt 0;
    padding: 4pt 0 4pt 10pt;
    color: #444;
    font-style: italic;
    background: #fafafa;
}
hr {
    border: none;
    border-top: 1px solid #ccc;
    margin: 12pt 0;
}
ul, ol {
    padding-left: 16pt;
    margin: 4pt 0;
}
li { margin: 2pt 0; }
p { margin: 4pt 0; }
strong { font-weight: bold; }
em { font-style: italic; }

/* Print optimizations */
@media print {
    body { 
        font-size: 8.5pt;
        width: 277mm;  /* A4 landscape (297mm) minus margins (2×10mm) */
    }
    pre { font-size: 5.5pt; }
    table { font-size: 7.5pt; }
    h1 { font-size: 14pt; }
    h2 { font-size: 11pt; page-break-after: avoid; }
    h3 { font-size: 9pt; page-break-after: avoid; }
    table, pre { page-break-inside: avoid; }
}
"""


def find_chrome():
    """Find Chrome/Edge executable for headless PDF generation."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def md_to_pdf(md_path: str, pdf_path: str = None):
    """Convert Markdown to PDF via HTML + Chrome/Edge headless."""
    md_path = Path(md_path)
    if not md_path.exists():
        print(f"Error: File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    if pdf_path is None:
        pdf_path = md_path.with_suffix(".pdf")
    else:
        pdf_path = Path(pdf_path)

    # Read and convert markdown to HTML
    md_text = md_path.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark", {"breaks": True}).enable("table")
    html_body = md.render(md_text)

    # Build full HTML document
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>{md_path.stem}</title>
<style>
{CSS}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""

    # Write intermediate HTML
    html_path = pdf_path.with_suffix(".html")
    html_path.write_text(html_doc, encoding="utf-8")
    print(f"HTML generated: {html_path}")

    # Try Chrome/Edge headless for PDF
    browser = find_chrome()
    if browser:
        print(f"Using: {Path(browser).name}")
        
        # Chrome headless --print-to-pdf defaults to portrait A4 with 800px viewport.
        # We set paper to landscape (11.69 x 8.27 inches) and force body width to match.
        # A4 landscape at 96dpi: (297-20)mm * 96/25.4 ≈ 1047px content width
        
        # Rewrite HTML to force layout at landscape width
        landscape_html = html_doc.replace(
            '<body>',
            '<body style="width:1047px; max-width:1047px; margin:0 auto;">'
        )
        html_path.write_text(landscape_html, encoding="utf-8")
        
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            "--print-to-pdf-paper-width=11.69",
            "--print-to-pdf-paper-height=8.27",
            f"--print-to-pdf={pdf_path}",
            str(html_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if not (pdf_path.exists() and pdf_path.stat().st_size > 0):
            # Fallback: old headless
            cmd[2] = "--headless"
            cmd = [c for c in cmd if c != "--no-pdf-header-footer"]
            cmd.insert(4, "--print-to-pdf-no-header")
            result = subprocess.run(cmd, capture_output=True, timeout=30)

        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            print(f"PDF generated: {pdf_path}")
            print(f"  File size: {pdf_path.stat().st_size:,} bytes")
            return
        else:
            print(f"  Chrome/Edge PDF generation failed: {result.stderr.decode()[:200]}", file=sys.stderr)

    # Fallback: just leave the HTML
    print(f"\nNo PDF engine available. HTML file ready at:")
    print(f"  {html_path}")
    print(f"  Open in browser and Print > Save as PDF (Ctrl+P)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python md_to_pdf.py <input.md> [output.pdf]")
        sys.exit(1)

    input_md = sys.argv[1]
    output_pdf = sys.argv[2] if len(sys.argv) > 2 else None
    md_to_pdf(input_md, output_pdf)
