"""Web pages, Markdown, email and plain text.

Websites hide things for good reasons all the time (menus, dialogs,
screen-reader text), so hiding comes in two strengths here:

  concealed: tricks with no honest use for prose, like zero-size or
             transparent text, text the colour of its background, or text
             pushed thousands of pixels off-screen.
  unseen:    ordinary hiding (display:none, the hidden attribute, comments,
             alt text). Only reported when it contains instructions for an AI.
"""

import email
import email.policy
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from ..core import CONCEALED, UNSEEN, VISIBLE, Segment

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "math", "head", "title"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
NAMED = {"white": (255, 255, 255), "#fff": (255, 255, 255), "#ffffff": (255, 255, 255), "black": (0, 0, 0),
         "transparent": None}


def _color(v: str) -> Optional[Tuple[int, int, int]]:
    v = v.strip().lower()
    if v in NAMED:
        return NAMED[v]
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return tuple(int(ch * 2, 16) for ch in m.group(1))
    m = re.fullmatch(r"#([0-9a-f]{6})(?:[0-9a-f]{2})?", v)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)(?:[\s,/]+([\d.]+%?))?\s*\)", v)
    if m:
        alpha = m.group(4)
        if alpha is not None and alpha.rstrip("%") in ("0", "0.0"):
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _close(a, b, tol=24):
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _px(v: str) -> Optional[float]:
    m = re.match(r"\s*(-?[\d.]+)\s*(px|pt|em|rem|%)?\s*$", v)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "px"
    return {"px": n, "pt": n * 1.33, "em": n * 16, "rem": n * 16, "%": n * 0.16}[unit]


def parse_style(style: str) -> Dict[str, str]:
    out = {}
    for decl in style.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def judge_style(st: Dict[str, str], inherited_bg) -> Tuple[Optional[str], Optional[str], object]:
    """(concealed_reason, unseen_reason, background_for_children)"""
    bg = inherited_bg
    for key in ("background-color", "background"):
        if key in st:
            c = _color(st[key].split()[0]) if st[key] else None
            if c is not None:
                bg = c
    if st.get("display") == "none":
        return None, "display:none", bg
    if st.get("visibility") in ("hidden", "collapse"):
        return None, "visibility:hidden", bg
    if st.get("opacity") in ("0", "0.0", "0%"):
        return "opacity 0", None, bg
    fs = _px(st.get("font-size", "")) if "font-size" in st else None
    if fs is not None and fs <= 2:
        return f"font-size {st['font-size']}", None, bg
    for prop in ("left", "top", "text-indent", "margin-left"):
        v = _px(st.get(prop, "")) if prop in st else None
        if v is not None and v <= -999:
            return f"moved off-screen ({prop}: {st[prop]})", None, bg
    clip = st.get("clip", "") + st.get("clip-path", "")
    if re.search(r"rect\(\s*0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?[\s,]+0(?:px)?\s*\)|inset\(\s*(?:50|100)%", clip):
        return None, "clipped (screen-reader text)", bg
    if "color" in st:
        c = _color(st["color"])
        if st["color"].strip() == "transparent":
            return "transparent text", None, bg
        if c is not None and bg is not None and _close(c, bg):
            return "text the same colour as its background", None, bg
        if c is not None and bg is None and _close(c, (255, 255, 255)):
            return None, "white text (page background unknown)", bg
    return None, None, bg


class _Sheet:
    """Just enough CSS: .class, #id and tag selectors, including comma lists."""

    def __init__(self):
        self.rules: List[Tuple[str, Dict[str, str]]] = []

    def feed(self, css: str):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            decl = parse_style(body)
            for s in sel.split(","):
                s = s.strip()
                if re.fullmatch(r"[.#]?[A-Za-z_][\w-]*", s):
                    self.rules.append((s, decl))

    def match(self, tag: str, attrs: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        classes = set((attrs.get("class") or "").split())
        for sel, decl in self.rules:
            if (sel.startswith(".") and sel[1:] in classes) or (sel.startswith("#") and sel[1:] == attrs.get("id")) \
                    or sel == tag:
                out.update(decl)
        return out


class _Html(HTMLParser):
    def __init__(self, sheet: _Sheet, base_line: int = 0):
        super().__init__(convert_charrefs=True)
        self.sheet = sheet
        self.stack: List[Tuple[str, Optional[str], Optional[str], object]] = []  # tag, concealed, unseen, bg
        self.skip = 0
        self.segments: List[Segment] = []
        self.buf: List[str] = []
        self.buf_state: Tuple[str, str, int] = (VISIBLE, "", 0)
        self.base_line = base_line

    def _state(self):
        for tag, conc, unseen, _ in reversed(self.stack):
            if conc:
                return CONCEALED, conc
        for tag, conc, unseen, _ in reversed(self.stack):
            if unseen:
                return UNSEEN, unseen
        return VISIBLE, ""

    def _flush(self):
        text = " ".join("".join(self.buf).split())
        if text:
            vis, why, line = self.buf_state
            self.segments.append(Segment(text, f"line {line}", vis, why))
        self.buf = []

    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}
        line = self.getpos()[0] + self.base_line
        for a in ("alt", "title", "aria-label", "placeholder"):
            if len(attrs.get(a, "")) > 20:
                self.segments.append(Segment(attrs[a], f"line {line} <{tag} {a}>", UNSEEN, f"{a} attribute"))
        if tag == "meta" and attrs.get("content") and attrs.get("name", "").lower() in ("description", "keywords", "author"):
            self.segments.append(Segment(attrs["content"], f"line {line} <meta {attrs.get('name')}>", UNSEEN, "page metadata"))
        if tag in VOID:
            return
        if tag in SKIP_TAGS:
            self.skip += 1
            self.stack.append((tag, None, None, self.stack[-1][3] if self.stack else None))
            return
        st = self.sheet.match(tag, attrs)
        st.update(parse_style(attrs.get("style", "")))
        parent_bg = self.stack[-1][3] if self.stack else None
        conc, unseen, bg = judge_style(st, parent_bg)
        if "hidden" in attrs and not unseen and not conc:
            unseen = "hidden attribute"
        if attrs.get("aria-hidden") == "true" and not unseen and not conc:
            unseen = None  # decorative; visible to sighted readers
        self._flush()
        self.stack.append((tag, conc, unseen, bg))
        vis, why = self._state()
        self.buf_state = (vis, why, line)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if tag in SKIP_TAGS:
                    self.skip = max(0, self.skip - 1)
                self._flush()
                del self.stack[i:]
                vis, why = self._state()
                self.buf_state = (vis, why, self.getpos()[0] + self.base_line)
                return

    def handle_data(self, data):
        if self.skip:
            return
        if not self.buf:
            vis, why = self._state()
            self.buf_state = (vis, why, self.getpos()[0] + self.base_line)
        self.buf.append(data)

    def handle_comment(self, data):
        if data.strip():
            self.segments.append(Segment(data.strip(), f"line {self.getpos()[0] + self.base_line}", UNSEEN, "HTML comment"))

    def close(self):
        super().close()
        self._flush()


def read_html(text: str) -> List[Segment]:
    sheet = _Sheet()
    for css in re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S | re.I):
        sheet.feed(css)
    p = _Html(sheet)
    p.feed(text)
    p.close()
    return p.segments


_MD_COMMENT = re.compile(r"^\s*\[(?://|comment|#)\]:\s*(?:#|<>)\s*[(\"'](.*)[)\"']\s*$", re.M | re.I)


def read_markdown(text: str) -> List[Segment]:
    out = []
    for m in _MD_COMMENT.finditer(text):
        out.append(Segment(m.group(1), f"line {text.count(chr(10), 0, m.start()) + 1}", UNSEEN, "Markdown comment"))
    body = _MD_COMMENT.sub("", text)
    # Markdown allows raw HTML, so the HTML reader sees comments, styled spans and the prose itself
    out.extend(read_html(body))
    return out


def read_text(text: str) -> List[Segment]:
    out = []
    line = 1
    for block in re.split(r"(\n\s*\n)", text):
        if block.strip():
            out.append(Segment(block, f"line {line}", VISIBLE))
        line += block.count("\n")
    return out


def read_email(data: bytes) -> List[Segment]:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    out = [Segment(str(msg.get("subject", "")), "subject", VISIBLE)]
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart():
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        if not isinstance(content, str):
            continue
        if ctype == "text/html":
            out.extend(Segment(s.text, f"HTML part {s.location}", s.visibility, s.reason) for s in read_html(content))
        elif ctype == "text/plain":
            out.extend(Segment(s.text, f"text part {s.location}", s.visibility, s.reason) for s in read_text(content))
    return out
