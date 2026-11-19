"""Word, Excel and PowerPoint files (Office Open XML).

These are zip files of XML, so hiding tricks show up as plain attributes:
white colour, the `vanish` flag, a 1pt font, a hidden sheet. Styles are
resolved the way Word does it, so text hidden through a style is caught too.
"""

import io
import posixpath
import re
import zipfile
import zlib
from typing import Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..core import CONCEALED, UNSEEN, VISIBLE, Segment

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
V = "{urn:schemas-microsoft-com:vml}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"

MAX_PART = 50 * 1024 * 1024
TINY_PT = 2.0


class OfficeError(Exception):
    pass


def _xml(zf: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise OfficeError(f"{name} is too large to scan")
    try:
        data = zf.read(info)
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError) as e:
        raise OfficeError(f"{name} is corrupted: {e}") from e
    # Office files never need a DTD; refusing one rules out entity-expansion bombs
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:
        raise OfficeError(f"{name} declares a DTD, which Office files never do")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise OfficeError(f"{name}: {e}") from e


def _rgb(val: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not val or not re.fullmatch(r"[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8}", val):
        return None
    v = val[-6:]
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _close(a, b, tol: int = 24) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


WHITE = (255, 255, 255)


def _text(el: ET.Element, tag: str) -> str:
    return "".join(t.text or "" for t in el.iter(tag))


def _core_props(zf: zipfile.ZipFile) -> Iterator[Segment]:
    for part in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        root = _xml(zf, part)
        if root is None:
            continue
        for el in root.iter():
            if el.text and len(el.text.strip()) > 20:
                field = el.tag.split("}")[-1]
                if el.tag.endswith("}vt:lpwstr") or field in ("lpwstr",):
                    field = "custom property"
                yield Segment(el.text.strip(), f"{part} ({field})", UNSEEN, "document properties")


# ------------------------------------------------------------------ Word

class _WordStyles:
    """Resolve run formatting through docDefaults, paragraph and character styles."""

    def __init__(self, root: Optional[ET.Element]):
        self.styles: Dict[str, Tuple[dict, Optional[str]]] = {}
        self.defaults: dict = {}
        if root is None:
            return
        dd = root.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        if dd is not None:
            self.defaults = self.props(dd)
        for st in root.iter(f"{W}style"):
            sid = st.get(f"{W}styleId")
            rpr = st.find(f"{W}rPr")
            based = st.find(f"{W}basedOn")
            self.styles[sid] = (self.props(rpr) if rpr is not None else {},
                                based.get(f"{W}val") if based is not None else None)

    @staticmethod
    def props(rpr: ET.Element) -> dict:
        out = {}
        for tag in ("vanish", "specVanish", "webHidden"):
            el = rpr.find(f"{W}{tag}")
            if el is not None:
                out["hidden"] = el.get(f"{W}val", "true") not in ("0", "false", "off")
        c = rpr.find(f"{W}color")
        if c is not None:
            theme = c.get(f"{W}themeColor", "")
            out["color"] = WHITE if theme in ("background1", "light1") else _rgb(c.get(f"{W}val"))
        sz = rpr.find(f"{W}sz")
        if sz is not None and (sz.get(f"{W}val") or "").isdigit():
            out["size"] = int(sz.get(f"{W}val")) / 2
        shd = rpr.find(f"{W}shd")
        if shd is not None:
            out["shade"] = _rgb(shd.get(f"{W}fill"))
        hl = rpr.find(f"{W}highlight")
        if hl is not None:
            out["shade"] = WHITE if hl.get(f"{W}val") == "white" else out.get("shade")
        return out

    def chain(self, sid: Optional[str], depth: int = 0) -> dict:
        if not sid or sid not in self.styles or depth > 20:
            return {}
        own, based = self.styles[sid]
        merged = dict(self.chain(based, depth + 1))
        merged.update(own)
        return merged

    def resolve(self, pstyle: Optional[str], rstyle: Optional[str], direct: dict) -> dict:
        out = dict(self.defaults)
        out.update(self.chain(pstyle))
        out.update(self.chain(rstyle))
        out.update(direct)
        return out


def _why_hidden(props: dict, background) -> Optional[str]:
    if props.get("hidden"):
        return "hidden-text formatting"
    size = props.get("size")
    if size is not None and size <= TINY_PT:
        return f"{size:g}pt font"
    color = props.get("color")
    bg = props.get("shade") or background
    if color is not None and _close(color, bg):
        return "white text on a white background" if _close(color, WHITE) else "text the same colour as its background"
    return None


def _container_background(p, parent, page_bg):
    """(background colour, is_unknown) for a paragraph, looking at its enclosing cell or shape."""
    node = parent.get(p)
    while node is not None:
        if node.tag == f"{W}tc":
            shd = node.find(f"{W}tcPr/{W}shd")
            fill = _rgb(shd.get(f"{W}fill")) if shd is not None else None
            if fill is not None:
                return fill, False
            return page_bg, _table_styled(node, parent)
        if node.tag == f"{WPS}wsp":
            sp = node.find(f"{WPS}spPr")
            if sp is not None:
                solid = sp.find(f"{A}solidFill/{A}srgbClr")
                if solid is not None:
                    return _rgb(solid.get("val")) or page_bg, False
                if sp.find(f"{A}noFill") is not None:
                    return page_bg, False
            return page_bg, True  # filled by a theme style
        if node.tag in (f"{V}shape", f"{V}rect", f"{V}roundrect", f"{V}oval"):
            if node.get("filled") in ("f", "false"):
                return page_bg, False
            fc = node.get("fillcolor", "")
            m = re.match(r"#?([0-9A-Fa-f]{6})", fc)
            if m:
                return _rgb(m.group(1)), False
            return page_bg, True
        node = parent.get(node)
    return page_bg, False


def _own_runs(p, parent):
    """Runs whose nearest paragraph is p: text inside a text box belongs to the text box's paragraphs."""
    for r in p.iter(f"{W}r"):
        node = parent.get(r)
        while node is not None and node.tag != f"{W}p":
            node = parent.get(node)
        if node is p:
            yield r


def _table_styled(tc, parent) -> bool:
    node = parent.get(tc)
    while node is not None and node.tag != f"{W}tbl":
        node = parent.get(node)
    return node is not None and node.find(f"{W}tblPr/{W}tblStyle") is not None


def read_docx(zf: zipfile.ZipFile) -> Iterator[Segment]:
    styles = _WordStyles(_xml(zf, "word/styles.xml"))
    doc = _xml(zf, "word/document.xml")
    if doc is None:
        raise OfficeError("no word/document.xml")
    bg = doc.find(f"{W}background")
    background = _rgb(bg.get(f"{W}color")) if bg is not None else WHITE
    background = background or WHITE

    parts = ["word/document.xml"] + sorted(n for n in zf.namelist()
                                           if re.match(r"word/(header|footer|footnotes|endnotes)\d*\.xml$", n))
    for part in parts:
        root = doc if part == "word/document.xml" else _xml(zf, part)
        if root is None:
            continue
        label = posixpath.basename(part)[:-4]
        parent = {c: p for p in root.iter() for c in p}
        for n, p in enumerate(root.iter(f"{W}p"), 1):
            # What's behind this paragraph: a shaded table cell, a filled shape, or the page
            local_bg, unknown_bg = _container_background(p, parent, background)
            ppr = p.find(f"{W}pPr")
            pstyle = None
            pshade = None
            if ppr is not None:
                ps = ppr.find(f"{W}pStyle")
                pstyle = ps.get(f"{W}val") if ps is not None else None
                shd = ppr.find(f"{W}shd")
                pshade = _rgb(shd.get(f"{W}fill")) if shd is not None else None
            visible, hidden, soft, reasons = [], [], [], set()
            for r in _own_runs(p, parent):
                text = "".join((t.text or "") for t in r if t.tag == f"{W}t")
                if not text:
                    continue
                rpr = r.find(f"{W}rPr")
                rstyle = None
                direct = {}
                if rpr is not None:
                    rs = rpr.find(f"{W}rStyle")
                    rstyle = rs.get(f"{W}val") if rs is not None else None
                    direct = _WordStyles.props(rpr)
                props = styles.resolve(pstyle, rstyle, direct)
                why = _why_hidden(props, pshade or local_bg)
                if why and unknown_bg and why.startswith(("white text", "text the same colour")) and not props.get("shade"):
                    soft.append(text)  # background comes from a style we can't see
                elif why:
                    hidden.append(text)
                    reasons.add(why)
                else:
                    visible.append(text)
            loc = f"{label} paragraph {n}"
            if hidden:
                yield Segment("".join(hidden), loc, CONCEALED, ", ".join(sorted(reasons)))
            if soft:
                yield Segment("".join(soft), loc, UNSEEN, "white text whose background is set by a shape or table style")
            if visible:
                yield Segment("".join(visible), loc, VISIBLE)
        for d in root.iter(f"{WP}docPr"):
            alt = " ".join(x for x in (d.get("title"), d.get("descr")) if x)
            if alt.strip():
                yield Segment(alt, f"{label} image '{d.get('name', '')}'", UNSEEN, "image alt text")

    comments = _xml(zf, "word/comments.xml")
    if comments is not None:
        for c in comments.iter(f"{W}comment"):
            yield Segment(_text(c, f"{W}t"), f"comment by {c.get(f'{W}author', 'unknown')}", UNSEEN, "a review comment")
    yield from _core_props(zf)


# ------------------------------------------------------------------ Excel

def _rels(zf: zipfile.ZipFile, part: str) -> Dict[str, str]:
    base = posixpath.dirname(part)
    rels = _xml(zf, posixpath.join(base, "_rels", posixpath.basename(part) + ".rels"))
    out = {}
    if rels is not None:
        for r in rels.iter(f"{PR}Relationship"):
            target = r.get("Target", "")
            out[r.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    return out


def read_xlsx(zf: zipfile.ZipFile) -> Iterator[Segment]:
    shared: List[str] = []
    ss = _xml(zf, "xl/sharedStrings.xml")
    if ss is not None:
        for si in ss.findall(f"{S}si"):
            shared.append(_text(si, f"{S}t"))

    fonts: List[dict] = []
    fills: List[Optional[tuple]] = []
    xf_font: List[Tuple[int, int]] = []
    st = _xml(zf, "xl/styles.xml")
    if st is not None:
        for f in st.findall(f"{S}fonts/{S}font"):
            d = {}
            c = f.find(f"{S}color")
            if c is not None:
                d["color"] = WHITE if c.get("theme") == "0" else _rgb(c.get("rgb"))
            sz = f.find(f"{S}sz")
            if sz is not None:
                try:
                    d["size"] = float(sz.get("val"))
                except (TypeError, ValueError):
                    pass
            fonts.append(d)
        for fl in st.findall(f"{S}fills/{S}fill"):
            pf = fl.find(f"{S}patternFill")
            fg = pf.find(f"{S}fgColor") if pf is not None else None
            if fg is not None and fg.get("rgb"):
                fills.append(_rgb(fg.get("rgb")))
            elif pf is not None and pf.get("patternType") not in (None, "none"):
                fills.append("unknown")  # theme or indexed colour we don't resolve
            else:
                fills.append(None)
        for xf in st.findall(f"{S}cellXfs/{S}xf"):
            xf_font.append((int(xf.get("fontId", "0")), int(xf.get("fillId", "0"))))

    wb = _xml(zf, "xl/workbook.xml")
    if wb is None:
        raise OfficeError("no xl/workbook.xml")
    rels = _rels(zf, "xl/workbook.xml")
    for sheet in wb.iter(f"{S}sheet"):
        name = sheet.get("name", "?")
        state = sheet.get("state", "visible")
        path = rels.get(sheet.get(f"{R}id"))
        root = _xml(zf, path) if path else None
        if root is None:
            continue
        hidden_cols = []
        for col in root.iter(f"{S}col"):
            if col.get("hidden") in ("1", "true"):
                hidden_cols.append((int(col.get("min", 0)), int(col.get("max", 0))))
        for row in root.iter(f"{S}row"):
            row_hidden = row.get("hidden") in ("1", "true")
            for c in row.findall(f"{S}c"):
                t = c.get("t")
                v = c.find(f"{S}v")
                if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    text = shared[int(v.text)]
                elif t == "inlineStr":
                    text = _text(c, f"{S}t")
                elif t == "str" and v is not None:
                    text = v.text or ""
                else:
                    continue
                if not text.strip():
                    continue
                ref = c.get("r", "?")
                loc = f"sheet '{name}' cell {ref}"
                col_n = _col_number(ref)
                if state != "visible":
                    yield Segment(text, loc, UNSEEN, f"{'very ' if state == 'veryHidden' else ''}hidden sheet")
                    continue
                if row_hidden or any(a <= col_n <= b for a, b in hidden_cols):
                    yield Segment(text, loc, UNSEEN, "hidden row or column")
                    continue
                s = int(c.get("s", "0"))
                why = None
                if s < len(xf_font):
                    fid, flid = xf_font[s]
                    font = fonts[fid] if fid < len(fonts) else {}
                    bg = fills[flid] if flid < len(fills) and fills[flid] else WHITE
                    if font.get("size") is not None and font["size"] <= TINY_PT:
                        why = f"{font['size']:g}pt font"
                    elif bg == "unknown":
                        if _close(font.get("color"), WHITE):
                            yield Segment(text, loc, UNSEEN, "white text on a theme-coloured cell")
                            continue
                    elif _close(font.get("color"), bg):
                        why = "font colour matches the cell background"
                yield Segment(text, loc, CONCEALED if why else VISIBLE, why or "")
    for part in sorted(n for n in zf.namelist() if re.match(r"xl/comments\d*\.xml$", n)):
        root = _xml(zf, part)
        for cm in (root.iter(f"{S}comment") if root is not None else []):
            yield Segment(_text(cm, f"{S}t"), f"comment on {cm.get('ref', '?')}", UNSEEN, "a cell comment")
    yield from _core_props(zf)


def _col_number(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n


# ------------------------------------------------------------------ PowerPoint

def read_pptx(zf: zipfile.ZipFile) -> Iterator[Segment]:
    pres = _xml(zf, "ppt/presentation.xml")
    cx = cy = None
    if pres is not None:
        sz = pres.find(f"{P}sldSz")
        if sz is not None:
            cx, cy = int(sz.get("cx", 0)), int(sz.get("cy", 0))
    slides = sorted((n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)", posixpath.basename(n)).group(1)))
    for path in slides:
        num = re.search(r"(\d+)", posixpath.basename(path)).group(1)
        root = _xml(zf, path)
        if root is None:
            continue
        hidden_slide = root.get("show") in ("0", "false")
        bg_white = False
        bgfill = root.find(f".//{P}bg//{A}srgbClr")
        if bgfill is not None and _close(_rgb(bgfill.get("val")), WHITE):
            bg_white = True
        for sp in root.iter(f"{P}sp"):
            off = sp.find(f".//{A}xfrm/{A}off")
            ext = sp.find(f".//{A}xfrm/{A}ext")
            offslide = False
            if off is not None and cx:
                x, y = int(off.get("x", 0)), int(off.get("y", 0))
                w = int(ext.get("cx", 0)) if ext is not None else 0
                h = int(ext.get("cy", 0)) if ext is not None else 0
                offslide = x >= cx or y >= cy or x + w <= 0 or y + h <= 0
            shape_filled = sp.find(f"{P}spPr/{A}solidFill") is not None
            for para_n, p in enumerate(sp.iter(f"{A}p"), 1):
                visible, hidden, reasons = [], [], set()
                for r in p.findall(f"{A}r"):
                    t = r.find(f"{A}t")
                    text = (t.text or "") if t is not None else ""
                    if not text:
                        continue
                    rpr = r.find(f"{A}rPr")
                    why = None
                    if offslide:
                        why = "placed outside the slide"
                    elif rpr is not None:
                        sz = rpr.get("sz")
                        if sz and sz.isdigit() and int(sz) / 100 <= TINY_PT:
                            why = f"{int(sz) / 100:g}pt font"
                        else:
                            clr = rpr.find(f"{A}solidFill/{A}srgbClr")
                            if bg_white and not shape_filled and clr is not None and _close(_rgb(clr.get("val")), WHITE):
                                why = "white text on a white slide"
                    (hidden if why else visible).append(text)
                    if why:
                        reasons.add(why)
                loc = f"slide {num}"
                if hidden_slide:
                    if hidden or visible:
                        yield Segment("".join(hidden + visible), loc, UNSEEN, "hidden slide")
                    continue
                if hidden:
                    yield Segment("".join(hidden), loc, CONCEALED, ", ".join(sorted(reasons)))
                if visible:
                    yield Segment("".join(visible), loc, VISIBLE)
        for d in root.iter(f"{P}cNvPr"):
            alt = " ".join(x for x in (d.get("title"), d.get("descr")) if x)
            if alt.strip():
                yield Segment(alt, f"slide {num} '{d.get('name', '')}'", UNSEEN, "alt text")
    for path in sorted(n for n in zf.namelist() if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)):
        root = _xml(zf, path)
        if root is None:
            continue
        text = " ".join(t.text or "" for t in root.iter(f"{A}t"))
        num = re.search(r"(\d+)", posixpath.basename(path)).group(1)
        yield Segment(text, f"notes {num}", UNSEEN, "speaker notes")
    yield from _core_props(zf)


def read(data: bytes, kind: str) -> List[Segment]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise OfficeError(f"not a valid Office file: {e}") from e
    with zf:
        if sum(i.file_size for i in zf.infolist()) > 500 * 1024 * 1024:
            raise OfficeError("expands to more than 500 MB")
        reader = {"docx": read_docx, "xlsx": read_xlsx, "pptx": read_pptx}[kind]
        return list(reader(zf))
