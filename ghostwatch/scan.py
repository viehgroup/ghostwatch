"""Pick a reader for each file, run it, and judge what it found."""

import io
import os
import zipfile
from typing import Iterator, List, Tuple

from .core import LOW, FileReport, Finding, Segment, judge
from .formats import ooxml, pdf, web

TEXT_EXT = {".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".rst", ".tex",
            ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".c", ".h", ".cpp", ".cs",
            ".sh", ".ps1", ".sql", ".xml", ".srt", ".vtt"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".tox", "vendor"}
MAX_FILE = 100 * 1024 * 1024


def kind_of(path: str, data: bytes) -> str:
    """Decide by content first: a renamed file is still what its bytes say."""
    head = data[:4096]
    ext = os.path.splitext(path)[1].lower()
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except (zipfile.BadZipFile, OSError):
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        return "unknown"
    if ext in (".html", ".htm", ".xhtml", ".svg"):
        return "html"
    if ext in (".md", ".markdown", ".mdx", ".mdc"):
        return "markdown"
    if ext in (".eml", ".mbox"):
        return "email"
    sniff = head[:2048].lstrip().lower()
    if sniff.startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext in TEXT_EXT or not ext:
        return "text" if _looks_text(head) else "unknown"
    return "text" if _looks_text(head) and ext not in (".png", ".jpg", ".gif", ".zip", ".exe") else "unknown"


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _looks_text(b: bytes) -> bool:
    if not b:
        return True
    if b"\x00" in b[:4096]:
        return False
    try:
        b[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.start > 4000  # cut mid-character at the boundary


def segments_for(path: str, data: bytes, kind: str) -> Tuple[List[Segment], List[str]]:
    if kind in ("docx", "xlsx", "pptx"):
        return ooxml.read(data, kind), []
    if kind == "pdf":
        return pdf.read(data)
    text = data.decode("utf-8", "replace")
    if kind == "html":
        return web.read_html(text), []
    if kind == "markdown":
        return web.read_markdown(text), []
    if kind == "email":
        return web.read_email(data), []
    return web.read_text(text), []


def scan_file(path: str, display: str = "") -> FileReport:
    display = display or path
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE:
            rep = FileReport(display, "skipped")
            rep.findings.append(Finding("GW012", LOW, display, "", f"file is larger than {MAX_FILE // 2**20} MB; not scanned"))
            return rep
        data = _read(path)
    except OSError as e:
        return FileReport(display, "unreadable", error=str(e))
    kind = kind_of(path, data)
    return scan_bytes(data, kind, display)


def scan_bytes(data: bytes, kind: str, display: str) -> FileReport:
    rep = FileReport(display, kind)
    if kind == "unknown":
        return rep
    try:
        segs, notes = segments_for(display, data, kind)
    except (ooxml.OfficeError, pdf.PdfError) as e:
        rep.error = str(e)
        rep.findings.append(Finding("GW012", LOW, display, "", f"couldn't read the file: {e}"))
        return rep
    except RecursionError:
        rep.error = "file is nested too deeply to parse"
        rep.findings.append(Finding("GW012", LOW, display, "", rep.error))
        return rep
    except Exception as e:  # hostile files must never take the scanner down
        rep.error = f"unexpected {type(e).__name__} while reading"
        rep.findings.append(Finding("GW012", LOW, display, "", f"couldn't read the file ({rep.error})",
                                    fix="Please report this file type so the reader can be improved."))
        return rep
    rep.segments = len(segs)
    rep.findings = judge(display, segs)
    for n in notes:
        rep.findings.append(Finding("GW012", LOW, display, "", n,
                                    fix="Hidden content in the parts that couldn't be read would be missed."))
    return rep


def walk(paths: List[str]) -> Iterator[str]:
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for fn in sorted(filenames):
                    yield os.path.join(dirpath, fn)
        else:
            yield p
