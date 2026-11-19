"""A small, dependency-free PDF reader that knows how text is drawn.

PDFs don't contain "text", they contain drawing instructions. To find
hidden text we replay those instructions and keep track of what matters:
render mode (mode 3 is invisible), fill colour, font size, position, and
what was painted underneath.

Two honest uses of invisible-looking text are handled on purpose:
  * OCR layers: scanned pages carry invisible text (mode 3) on top of the
    page image so it can be searched. Mode 3 over an image is treated as
    normal text.
  * White text on coloured boxes and images is normal design. White text is
    only reported when nothing dark was painted under it.

Not supported: encrypted PDFs (reported, not scanned), LZW, and fonts
whose text can't be mapped to Unicode.
"""

import base64
import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core import CONCEALED, UNSEEN, VISIBLE, Segment

MAX_STREAM = 30 * 1024 * 1024
MAX_PAGES = 1000
MAX_OPS = 2_000_000
TINY_PT = 1.5


class PdfError(Exception):
    pass


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int


class Name(str):
    pass


class Op(str):
    """A content-stream operator or bare keyword, kept apart from string operands."""


@dataclass
class Stream:
    dict: dict
    raw: bytes


# ------------------------------------------------------------------ lexer / parser

_WS = b" \t\r\n\x00\x0c"
_DELIM = b"()<>[]{}/%"
_NUM = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


class Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.i = pos

    def skip_ws(self):
        d, n = self.d, len(self.d)
        while self.i < n:
            c = d[self.i]
            if c in _WS:
                self.i += 1
            elif c == 0x25:  # % comment
                e = d.find(b"\n", self.i)
                self.i = n if e < 0 else e + 1
            else:
                break

    def token(self):
        """Return the next object or operator (as bytes)."""
        self.skip_ws()
        d = self.d
        if self.i >= len(d):
            return None
        c = d[self.i:self.i + 1]
        if c == b"/":
            j = self.i + 1
            while j < len(d) and d[j] not in _WS and d[j] not in _DELIM:
                j += 1
            raw = d[self.i + 1:j]
            self.i = j
            return Name(re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw).decode("latin-1"))
        if c == b"(":
            return self._literal()
        if c == b"<":
            if d[self.i + 1:self.i + 2] == b"<":
                self.i += 2
                return self._dict()
            j = d.find(b">", self.i)
            hexs = re.sub(rb"\s", b"", d[self.i + 1:j if j > 0 else len(d)])
            self.i = (j + 1) if j > 0 else len(d)
            if len(hexs) % 2:
                hexs += b"0"
            try:
                return bytes.fromhex(hexs.decode("latin-1"))
            except ValueError:
                return b""
        if c == b"[":
            self.i += 1
            arr = []
            while True:
                self.skip_ws()
                if self.i >= len(d):
                    return arr
                if d[self.i:self.i + 1] == b"]":
                    self.i += 1
                    return arr
                t = self.token()
                if t is None:
                    return arr
                arr.append(t)
                self._maybe_ref(arr)
        if c in (b">", b"]", b")", b"{", b"}"):
            self.i += 1
            return Op(c.decode())
        m = _NUM.match(d, self.i)
        if m:
            self.i = m.end()
            s = m.group(0)
            return float(s) if b"." in s else int(s)
        j = self.i
        while j < len(d) and d[j] not in _WS and d[j] not in _DELIM:
            j += 1
        if j == self.i:
            j += 1
        word = d[self.i:j]
        self.i = j
        if word == b"true":
            return True
        if word == b"false":
            return False
        if word == b"null":
            return None
        return Op(word.decode("latin-1"))

    @staticmethod
    def _maybe_ref(items: list):
        if len(items) >= 3 and isinstance(items[-1], Op) and items[-1] == "R" and isinstance(items[-2], int) and isinstance(items[-3], int) \
                and not isinstance(items[-2], bool) and not isinstance(items[-3], bool):
            items[-3:] = [Ref(items[-3], items[-2])]

    def _dict(self) -> dict:
        items = []
        d = self.d
        while True:
            self.skip_ws()
            if self.i >= len(d):
                break
            if d[self.i:self.i + 2] == b">>":
                self.i += 2
                break
            t = self.token()
            if t is None:
                break
            items.append(t)
            self._maybe_ref(items)
        out = {}
        for k in range(0, len(items) - 1, 2):
            if isinstance(items[k], Name):
                out[str(items[k])] = items[k + 1]
        return out

    def _literal(self) -> bytes:
        d = self.d
        i = self.i + 1
        depth = 1
        out = bytearray()
        esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        while i < len(d) and depth:
            c = d[i]
            if c == 0x5C:  # backslash
                i += 1
                if i >= len(d):
                    break
                c = d[i]
                if c in esc:
                    out.append(esc[c])
                elif 0x30 <= c <= 0x37:
                    m = re.match(rb"[0-7]{1,3}", d[i:i + 3])
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += len(m.group(0)) - 1
                elif c in (0x0D, 0x0A):
                    if c == 0x0D and d[i + 1:i + 2] == b"\n":
                        i += 1
                else:
                    out.append(c)
            elif c == 0x28:
                depth += 1
                out.append(c)
            elif c == 0x29:
                depth -= 1
                if depth:
                    out.append(c)
            else:
                out.append(c)
            i += 1
        self.i = i
        return bytes(out)


# ------------------------------------------------------------------ document

# Bounded digit counts: unbounded \d+ backtracks quadratically on long runs of digits
_OBJ = re.compile(rb"(?<![\d.])(\d{1,10})\s{1,10}(\d{1,5})\s{1,10}obj\b")


def _decode_stream(s: Stream, doc: "Document") -> bytes:
    data = s.raw
    filters = doc.resolve(s.dict.get("Filter"))
    parms = doc.resolve(s.dict.get("DecodeParms"))
    if isinstance(filters, Name):
        filters, parms = [filters], [parms]
    filters = filters or []
    if not isinstance(parms, list):
        parms = [parms] * len(filters)
    for f, p in zip(filters, parms + [None] * (len(filters) - len(parms))):
        f = str(f)
        if f in ("FlateDecode", "Fl"):
            try:
                d = zlib.decompressobj()
                data = d.decompress(data, MAX_STREAM)
            except zlib.error:
                try:
                    data = zlib.decompressobj(-15).decompress(data[2:], MAX_STREAM)
                except zlib.error as e:
                    raise PdfError(f"bad Flate data: {e}") from e
            p = doc.resolve(p) if p is not None else None
            if isinstance(p, dict) and isinstance(p.get("Predictor"), int) and p["Predictor"] >= 10:
                data = _png_unpredict(data, int(p.get("Columns", 1)) * int(p.get("Colors", 1)) * int(p.get("BitsPerComponent", 8)) // 8)
        elif f in ("ASCIIHexDecode", "AHx"):
            h = re.sub(rb"\s|>.*", b"", data, flags=re.S)
            try:
                data = bytes.fromhex((h + (b"0" if len(h) % 2 else b"")).decode("latin-1"))
            except ValueError as e:
                raise PdfError(f"bad hex data: {e}") from e
        elif f in ("ASCII85Decode", "A85"):
            body = data.strip()
            body = body[2:] if body.startswith(b"<~") else body
            body = body.split(b"~>")[0]
            try:
                data = base64.a85decode(re.sub(rb"\s", b"", body))
            except ValueError as e:
                raise PdfError(f"bad ASCII85 data: {e}") from e
        else:
            raise PdfError(f"unsupported filter {f}")
    return data


def _png_unpredict(data: bytes, cols: int) -> bytes:
    if cols <= 0:
        return data
    out = bytearray()
    prev = bytearray(cols)
    row = cols + 1
    for k in range(0, len(data) - row + 1, row):
        ft, line = data[k], bytearray(data[k + 1:k + row])
        for x in range(cols):
            left = line[x - 1] if x else 0
            up = prev[x]
            ul = prev[x - 1] if x else 0
            if ft == 1:
                line[x] = (line[x] + left) & 0xFF
            elif ft == 2:
                line[x] = (line[x] + up) & 0xFF
            elif ft == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif ft == 4:
                pa, pb, pc = abs(up - ul), abs(left - ul), abs(left + up - 2 * ul)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else ul)
                line[x] = (line[x] + pred) & 0xFF
        out += line
        prev = line
    return bytes(out)


class Document:
    def __init__(self, data: bytes):
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise PdfError("not a PDF file")
        self.data = data
        self.offsets: Dict[int, int] = {}
        for m in _OBJ.finditer(data):
            self.offsets[int(m.group(1))] = m.end()  # later definitions (incremental updates) win
        self.cache: Dict[int, object] = {}
        self.objstm: Dict[int, Tuple[int, int]] = {}
        self.trailer: dict = {}
        self._load_trailers()
        self._load_object_streams()

    def _load_trailers(self):
        for m in re.finditer(rb"trailer\s*<<", self.data):
            self.trailer.update(Lexer(self.data, m.end() - 2).token() or {})
        # cross-reference streams carry the trailer keys in their own dictionary
        for num in list(self.offsets):
            head = self.data[self.offsets[num]:self.offsets[num] + 200]
            if b"/XRef" in head:
                obj = self.get(num)
                if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
                    for k in ("Root", "Info", "Encrypt"):
                        if k in obj.dict and k not in self.trailer:
                            self.trailer[k] = obj.dict[k]

    def _load_object_streams(self):
        for num in list(self.offsets):
            head = self.data[self.offsets[num]:self.offsets[num] + 300]
            if b"/ObjStm" not in head:
                continue
            obj = self.get(num)
            if not isinstance(obj, Stream) or obj.dict.get("Type") != "ObjStm":
                continue
            try:
                body = _decode_stream(obj, self)
            except PdfError:
                continue
            n, first = int(obj.dict.get("N", 0)), int(obj.dict.get("First", 0))
            lx = Lexer(body)
            pairs = [lx.token() for _ in range(2 * n)]
            for k in range(0, len(pairs) - 1, 2):
                onum, off = pairs[k], pairs[k + 1]
                if isinstance(onum, int) and isinstance(off, int) and onum not in self.offsets:
                    self.objstm[onum] = (num, first + off)
            self._objstm_bodies = getattr(self, "_objstm_bodies", {})
            self._objstm_bodies[num] = body

    @property
    def encrypted(self) -> bool:
        return "Encrypt" in self.trailer

    def get(self, num: int):
        if num in self.cache:
            return self.cache[num]
        self.cache[num] = None  # guards against reference loops
        if num in self.offsets:
            lx = Lexer(self.data, self.offsets[num])
            obj = lx.token()
            lx.skip_ws()
            if isinstance(obj, dict) and self.data.startswith(b"stream", lx.i):
                start = lx.i + 6
                if self.data[start:start + 2] == b"\r\n":
                    start += 2
                elif self.data[start:start + 1] in (b"\n", b"\r"):
                    start += 1
                length = obj.get("Length")
                if isinstance(length, Ref):
                    length = self.get(length.num) if length.num != num else None
                end = start + length if isinstance(length, int) and 0 <= length <= MAX_STREAM else -1
                if end < 0 or self.data[end:end + 30].lstrip()[:9] != b"endstream":
                    e = self.data.find(b"endstream", start)
                    end = e if e >= 0 else len(self.data)
                obj = Stream(obj, self.data[start:end])
        elif num in self.objstm:
            sn, off = self.objstm[num]
            body = getattr(self, "_objstm_bodies", {}).get(sn, b"")
            obj = Lexer(body, off).token()
        else:
            obj = None
        self.cache[num] = obj
        return obj

    def resolve(self, obj, depth: int = 0):
        while isinstance(obj, Ref) and depth < 32:
            obj = self.get(obj.num)
            depth += 1
        return obj

    def stream_data(self, obj) -> bytes:
        obj = self.resolve(obj)
        if isinstance(obj, Stream):
            return _decode_stream(obj, self)
        return b""

    def pages(self) -> List[dict]:
        root = self.resolve(self.trailer.get("Root"))
        out: List[dict] = []
        if isinstance(root, dict):
            self._walk(self.resolve(root.get("Pages")), {}, out, set())
        if not out:  # damaged page tree: fall back to every /Type /Page object
            for num in sorted(self.offsets):
                o = self.get(num)
                if isinstance(o, dict) and o.get("Type") == "Page":
                    out.append(o)
        return out[:MAX_PAGES]

    def _walk(self, node, inherited, out, seen):
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        inh = dict(inherited)
        for k in ("Resources", "MediaBox", "CropBox"):
            if k in node:
                inh[k] = node[k]
        if node.get("Type") == "Page" or ("Contents" in node and "Kids" not in node):
            page = dict(inh)
            page.update(node)
            out.append(page)
            return
        for kid in self.resolve(node.get("Kids")) or []:
            self._walk(self.resolve(kid), inh, out, seen)


# ------------------------------------------------------------------ fonts

_GLYPHS = {"space": " ", "period": ".", "comma": ",", "hyphen": "-", "colon": ":", "semicolon": ";",
           "exclam": "!", "question": "?", "quotesingle": "'", "quotedbl": '"', "parenleft": "(",
           "parenright": ")", "slash": "/", "at": "@", "underscore": "_", "zero": "0", "one": "1", "two": "2",
           "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
           "quoteright": "'", "quoteleft": "'", "endash": "-", "emdash": "-", "ampersand": "&"}


class Font:
    def __init__(self, doc: Document, fobj):
        f = _d(doc.resolve(fobj))
        self.two_byte = str(f.get("Subtype")) == "Type0"
        # Glyph height relative to the font size. Standard fonts: 1. Type3 fonts define their own
        # units (FontMatrix [1 0 0 1] is 1000x a normal font), so use their declared glyph box;
        # None means we can't know, and the tiny-text rule is skipped for this font.
        self.scale: Optional[float] = 1.0
        if str(f.get("Subtype")) == "Type3":
            self.scale = None
            fm, bb = doc.resolve(f.get("FontMatrix")), doc.resolve(f.get("FontBBox"))
            try:
                units = abs(float(doc.resolve(bb[3])) - float(doc.resolve(bb[1])))
                if units > 1:  # boxes like [0 0 1 -1] are placeholders, not measurements
                    self.scale = units * abs(float(doc.resolve(fm[3])))
            except (TypeError, ValueError, IndexError):
                pass
        self.cmap: Dict[int, str] = {}
        self.unmapped = False
        if "ToUnicode" in f:
            try:
                self._parse_cmap(doc.stream_data(f["ToUnicode"]))
            except PdfError:
                pass
        enc = doc.resolve(f.get("Encoding"))
        if isinstance(enc, dict):
            code = 0
            for item in doc.resolve(enc.get("Differences")) or []:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, Name):
                    if code not in self.cmap:
                        g = str(item)
                        self.cmap[code] = g if len(g) == 1 else _GLYPHS.get(g, "")
                    code += 1
        if self.two_byte and not self.cmap:
            self.unmapped = True

    def _parse_cmap(self, data: bytes):
        text = data.decode("latin-1")

        def u(h: str) -> str:
            try:
                return bytes.fromhex(h).decode("utf-16-be", "replace")
            except ValueError:
                return ""
        for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
                self.cmap[int(src, 16)] = u(dst)
        for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
            for a, b, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block):
                lo, hi = int(a, 16), int(b, 16)
                if hi - lo > 65536:
                    continue
                if rest.startswith("["):
                    for k, h in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                        self.cmap[lo + k] = u(h)
                else:
                    h = rest[1:-1]
                    try:
                        base = int(h, 16)
                    except ValueError:
                        continue
                    width = len(h)
                    for k in range(hi - lo + 1):
                        self.cmap[lo + k] = u(format(base + k, f"0{width}x"))

    def decode(self, s: bytes) -> str:
        if self.two_byte:
            return "".join(self.cmap.get((s[k] << 8) | s[k + 1], "") for k in range(0, len(s) - 1, 2))
        if self.cmap:
            return "".join(self.cmap.get(b, chr(b) if 32 <= b < 127 else "") for b in s)
        return s.decode("latin-1")


# ------------------------------------------------------------------ content stream replay

def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _mul(a, b):
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _to_rgb(vals: List[float], space: int) -> Optional[Tuple[float, float, float]]:
    try:
        if space == 1 and len(vals) >= 1:
            return (vals[0], vals[0], vals[0])
        if space == 3 and len(vals) >= 3:
            return (vals[0], vals[1], vals[2])
        if space == 4 and len(vals) >= 4:
            c, m, y, k = vals[:4]
            return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    except TypeError:
        return None
    return None


def _white(rgb) -> bool:
    return rgb is not None and all(x >= 0.94 for x in rgb)


@dataclass
class _State:
    ctm: list = field(default_factory=lambda: [1, 0, 0, 1, 0, 0])
    fill: Optional[tuple] = (0, 0, 0)
    space: int = 1
    font: Optional[Font] = None
    size: float = 0
    render: int = 0
    leading: float = 0


class _Page:
    def __init__(self, doc: Document, page: dict, num: int):
        self.doc, self.num = doc, num
        self.media = self._box(page.get("MediaBox"), (0, 0, 612, 792))
        self.box = self._box(page.get("CropBox"), self.media)   # the part a viewer shows
        self.area = max(1.0, (self.box[2] - self.box[0]) * (self.box[3] - self.box[1]))
        self.covers: List[Tuple[float, float, float, float]] = []   # dark fills and images, in page space
        self.big_image = False
        self.pieces: List[Tuple[str, str, str, float, float]] = []    # (text, reason, visibility, y, size)
        self.ocr = False
        self.unmapped = False
        self.ops = 0

    def _box(self, raw, default):
        box = self.doc.resolve(raw)
        try:
            x0, y0, x1, y1 = [float(self.doc.resolve(v)) for v in box[:4]]
        except (TypeError, ValueError, IndexError):
            return default
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def run(self, resources, data: bytes, gs: _State, depth: int = 0):
        doc = self.doc
        res = _d(doc.resolve(resources))
        fonts = _d(doc.resolve(res.get("Font")))
        xobjs = _d(doc.resolve(res.get("XObject")))
        cspaces = _d(doc.resolve(res.get("ColorSpace")))
        font_cache: Dict[str, Font] = {}
        stack: List[_State] = []
        st = _State(list(gs.ctm), gs.fill, gs.space, gs.font, gs.size, gs.render, gs.leading)
        tm = [1, 0, 0, 1, 0, 0]
        tlm = [1, 0, 0, 1, 0, 0]
        path_rects: List[Tuple[float, float, float, float]] = []
        args: list = []
        lx = Lexer(data)
        while True:
            t = lx.token()
            if t is None:
                break
            if not isinstance(t, Op):
                args.append(t)
                Lexer._maybe_ref(args)
                continue
            op = str(t)
            if op == "BI":
                k = data.find(b"ID", lx.i)
                end = re.search(rb"\sEI(?=\s|$)", data[k + 2:]) if k >= 0 else None
                lx.i = (k + 2 + end.end()) if end else len(data)
                self._image(st.ctm)
                args = []
                continue
            self.ops += 1
            if self.ops > MAX_OPS:
                break
            try:
                self._op(op, args, st, stack, tm, tlm, fonts, font_cache, xobjs, cspaces, path_rects, depth)
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                pass
            if op == "BT":
                tm[:] = [1, 0, 0, 1, 0, 0]
                tlm[:] = [1, 0, 0, 1, 0, 0]
            args = []

    def _op(self, op, a, st, stack, tm, tlm, fonts, font_cache, xobjs, cspaces, path_rects, depth):
        doc = self.doc
        if op == "q":
            stack.append(_State(list(st.ctm), st.fill, st.space, st.font, st.size, st.render, st.leading))
        elif op == "Q" and stack:
            s = stack.pop()
            st.ctm, st.fill, st.space = s.ctm, s.fill, s.space
            st.font, st.size, st.render, st.leading = s.font, s.size, s.render, s.leading
        elif op == "cm" and len(a) >= 6:
            st.ctm = _mul([float(x) for x in a[-6:]], st.ctm)
        elif op == "g":
            st.space, st.fill = 1, _to_rgb([float(a[-1])], 1)
        elif op == "rg":
            st.space, st.fill = 3, _to_rgb([float(x) for x in a[-3:]], 3)
        elif op == "k":
            st.space, st.fill = 4, _to_rgb([float(x) for x in a[-4:]], 4)
        elif op == "cs":
            name = str(a[-1])
            if name in ("DeviceGray", "CalGray", "G"):
                st.space = 1
            elif name in ("DeviceRGB", "CalRGB", "RGB"):
                st.space = 3
            elif name in ("DeviceCMYK", "CMYK"):
                st.space = 4
            else:
                cs = doc.resolve(cspaces.get(name))
                st.space = 0
                if isinstance(cs, list) and cs and str(cs[0]) == "ICCBased":
                    icc = doc.resolve(cs[1])
                    if isinstance(icc, Stream):
                        st.space = int(icc.dict.get("N", 0))
            st.fill = (0, 0, 0) if st.space else None
        elif op in ("sc", "scn"):
            nums = [float(x) for x in a if isinstance(x, (int, float))]
            st.fill = _to_rgb(nums, st.space) if st.space else None
        elif op == "re" and len(a) >= 4:
            x, y, w, h = [float(v) for v in a[-4:]]
            path_rects.append(self._bbox(st.ctm, x, y, w, h))
        elif op in ("m", "l", "c", "v", "y") and len(a) >= 2:
            # general paths: collect every point; the filled area is their bounding box
            nums = [float(v) for v in a if isinstance(v, (int, float))]
            pts = [(nums[k], nums[k + 1]) for k in range(0, len(nums) - 1, 2)]
            m = st.ctm
            xs = [px * m[0] + py * m[2] + m[4] for px, py in pts]
            ys = [px * m[1] + py * m[3] + m[5] for px, py in pts]
            bb = (min(xs), min(ys), max(xs), max(ys))
            if path_rects and len(path_rects[-1]) == 5:
                o = path_rects[-1]
                path_rects[-1] = (min(o[0], bb[0]), min(o[1], bb[1]), max(o[2], bb[2]), max(o[3], bb[3]), "path")
            else:
                path_rects.append(bb + ("path",))
        elif op in ("f", "F", "f*", "B", "B*", "b", "b*"):
            if not _white(st.fill):
                for bb in path_rects:
                    self._cover(bb[:4])
            path_rects.clear()
        elif op == "sh":
            self._cover(self.box)  # a shading fills the current clip area, often the whole page
        elif op in ("n", "S", "s"):
            path_rects.clear()
        elif op == "Do":
            self._xobject(str(a[-1]), st, xobjs, depth)
        elif op == "Tf" and len(a) >= 2:
            name = str(a[-2])
            if name not in font_cache:
                font_cache[name] = Font(doc, fonts.get(name))
            st.font = font_cache[name]
            st.size = float(a[-1])
        elif op == "Tr":
            st.render = int(a[-1])
        elif op == "TL":
            st.leading = float(a[-1])
        elif op == "Tm" and len(a) >= 6:
            tm[:] = [float(x) for x in a[-6:]]
            tlm[:] = list(tm)
        elif op in ("Td", "TD") and len(a) >= 2:
            tx, ty = float(a[-2]), float(a[-1])
            if op == "TD":
                st.leading = -ty
            tlm[:] = _mul([1, 0, 0, 1, tx, ty], tlm)
            tm[:] = list(tlm)
        elif op == "T*":
            tlm[:] = _mul([1, 0, 0, 1, 0, -st.leading], tlm)
            tm[:] = list(tlm)
        elif op in ("Tj", "'", '"'):
            if op in ("'", '"'):
                tlm[:] = _mul([1, 0, 0, 1, 0, -st.leading], tlm)
                tm[:] = list(tlm)
            s = a[-1] if a and isinstance(a[-1], bytes) else b""
            self._show(st, tm, st.font.decode(s) if st.font else s.decode("latin-1"))
        elif op == "TJ" and a and isinstance(a[-1], list):
            parts = []
            for item in a[-1]:
                if isinstance(item, bytes):
                    parts.append(st.font.decode(item) if st.font else item.decode("latin-1"))
                elif isinstance(item, (int, float)) and item < -180:
                    parts.append(" ")
            self._show(st, tm, "".join(parts))

    @staticmethod
    def _bbox(m, x, y, w, h):
        pts = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
        xs = [px * m[0] + py * m[2] + m[4] for px, py in pts]
        ys = [px * m[1] + py * m[3] + m[5] for px, py in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def _xobject(self, name, st, xobjs, depth):
        xo = self.doc.resolve(xobjs.get(name))
        if not isinstance(xo, Stream):
            return
        sub = str(xo.dict.get("Subtype"))
        if sub == "Image":
            self._image(st.ctm)
        elif sub == "Form" and depth < 8:
            m = xo.dict.get("Matrix")
            ctm = _mul([float(v) for v in m], st.ctm) if isinstance(m, list) and len(m) == 6 else list(st.ctm)
            try:
                data = _decode_stream(xo, self.doc)
            except PdfError:
                return
            gs = _State(ctm, st.fill, st.space, st.font, st.size, st.render, st.leading)
            self.run(xo.dict.get("Resources"), data, gs, depth + 1)

    def _image(self, ctm):
        bb = self._bbox(ctm, 0, 0, 1, 1)
        self._cover(bb)
        if (bb[2] - bb[0]) * (bb[3] - bb[1]) >= 0.5 * self.area:
            self.big_image = True

    def _cover(self, bb):
        # hairlines and specks don't hide anything; and keep the list bounded on huge pages
        if (bb[2] - bb[0]) * (bb[3] - bb[1]) >= 4 and len(self.covers) < 5000:
            self.covers.append(bb)

    def _show(self, st: _State, tm, text: str):
        if not text:
            return
        if st.font and st.font.unmapped:
            self.unmapped = True
        m = _mul(tm, st.ctm)
        scale = (m[2] ** 2 + m[3] ** 2) ** 0.5
        eff = abs(st.size) * scale
        x, y = m[4], m[5]
        b = self.box
        reason = ""
        if st.render in (3, 7):
            if self.big_image or self._covered(x, y):
                self.ocr = True
            else:
                reason = "invisible text (render mode 3)"
        glyph = eff * st.font.scale if (st.font and st.font.scale is not None) else (None if st.font else eff)
        if not reason and glyph and glyph < TINY_PT and text.strip():
            reason = f"{glyph:.2g}pt text"
        if not reason:
            # Estimate where the run of text ends (about half an em per character along the
            # baseline) and only call it off-page if none of that span touches the page.
            run = len(text) * 0.5 * abs(st.size)
            ex, ey = x + m[0] * run, y + m[1] * run
            lo_x, hi_x, lo_y, hi_y = min(x, ex), max(x, ex), min(y, ey), max(y, ey)

            def outside(bx):
                return hi_x < bx[0] - 20 or lo_x > bx[2] + 20 or hi_y < bx[1] - 20 or lo_y > bx[3] + 20
            if outside(self.media):
                reason = "placed outside the page"
            elif outside(b):
                self.pieces.append((text, "in the trimmed margin, outside the visible page", UNSEEN, y, eff))
                return
        if not reason and _white(st.fill) and st.render in (0, 2, 4, 6) and not self._covered(x + eff * 0.3, y + eff * 0.3):
            reason = "white text with nothing behind it"
        self.pieces.append((text, reason, CONCEALED if reason else VISIBLE, y, eff))
        if not reason and st.fill is not None and not _white(st.fill) and st.render in (0, 2, 4, 6) and eff:
            # dark glyphs can be the background too (decorative bar fonts, drop shadows)
            run = len(text) * 0.5 * eff
            self._cover((x - eff * 0.2, y - eff * 0.3, x + run, y + eff * 1.1))

    def _covered(self, x, y) -> bool:
        return any(a <= x <= c and b <= y <= d for a, b, c, d in self.covers)


def read(data: bytes) -> Tuple[List[Segment], List[str]]:
    """Segments plus notes about anything that couldn't be scanned."""
    doc = Document(data)
    notes: List[str] = []
    segs: List[Segment] = []
    if doc.encrypted:
        notes.append("the PDF is encrypted, so its text couldn't be read")
        return segs, notes

    info = doc.resolve(doc.trailer.get("Info"))
    if isinstance(info, dict):
        for k in ("Title", "Subject", "Keywords", "Author", "Creator", "Producer"):
            v = doc.resolve(info.get(k))
            if isinstance(v, bytes) and len(v) > 20:
                segs.append(Segment(_pdf_string(v), f"document info ({k})", UNSEEN, "document metadata"))

    unmapped_pages = 0
    for n, page in enumerate(doc.pages(), 1):
        p = _Page(doc, page, n)
        contents = doc.resolve(page.get("Contents"))
        streams = contents if isinstance(contents, list) else [contents]
        data = b""
        for c in streams:
            try:
                data += doc.stream_data(c) + b"\n"
            except PdfError as e:
                notes.append(f"page {n}: {e}")
        p.run(page.get("Resources"), data, _State())
        if p.unmapped:
            unmapped_pages += 1
        parts, last_y = [], None
        for t, r, v, y, size in p.pieces:
            if v != VISIBLE:
                continue
            if last_y is not None and abs(y - last_y) > max(0.5 * size, 0.5):
                parts.append(" ")  # new line: keep words apart
            parts.append(t)
            last_y = y
        visible = "".join(parts)
        seen_text = _norm_words(visible)
        for (v, r), lines in _hidden_lines(p.pieces).items():
            fresh = []
            for line in lines:
                if v == CONCEALED and len(_norm_words(line)) >= 8 and _norm_words(line) in seen_text:
                    # hidden copy of text that's visible on the page (layout leftovers): nothing new
                    segs.append(Segment(line, f"page {n}", UNSEEN, r + ", duplicating visible text"))
                else:
                    fresh.append(line)
            if fresh:
                # joined back up so a sentence split across lines still reads as one
                segs.append(Segment(" ".join(fresh), f"page {n}", v, r))
        if visible.strip():
            segs.append(Segment(" ".join(visible.split()), f"page {n}", VISIBLE))
        annots = doc.resolve(page.get("Annots"))
        for annot in annots if isinstance(annots, list) else []:
            a = doc.resolve(annot)
            if isinstance(a, dict):
                c = doc.resolve(a.get("Contents"))
                if isinstance(c, bytes) and c.strip():
                    segs.append(Segment(_pdf_string(c), f"page {n} annotation", UNSEEN, "a PDF annotation"))
    if unmapped_pages:
        notes.append(f"text on {unmapped_pages} page(s) uses fonts without a Unicode map and couldn't be fully decoded")
    return segs, notes


def _hidden_lines(pieces) -> Dict[Tuple[str, str], List[str]]:
    """Hidden pieces grouped by why they're hidden, then into lines by baseline."""
    out: Dict[Tuple[str, str], List[str]] = {}
    last: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for text, reason, vis, y, size in pieces:
        if vis == VISIBLE:
            continue
        key = (vis, reason)
        lines = out.setdefault(key, [])
        prev = last.get(key)
        if lines and prev and abs(y - prev[0]) <= max(0.5 * max(size, prev[1]), 0.5):
            lines[-1] += text
        else:
            lines.append(text)
        last[key] = (y, size)
    return {k: [" ".join(t.split()) for t in v if t.strip()] for k, v in out.items()}


def _norm_words(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _pdf_string(b: bytes) -> str:
    if b[:2] in (b"\xfe\xff", b"\xff\xfe"):
        return b.decode("utf-16", "replace")
    return b.decode("latin-1")
