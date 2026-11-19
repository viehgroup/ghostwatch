"""Shared analysis: what counts as hidden, and what counts as an instruction.

Every format reader turns a file into Segments: pieces of text plus where
they are and, if a human wouldn't see them, why. Everything after that is
format-independent.
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

HIGH, MEDIUM, LOW = "high", "medium", "low"
SEVERITY_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}

# How text can be out of sight:
#   concealed: formatted so a reader won't see it (white text, tiny font, display:none...)
#   unseen:    lives somewhere people rarely look (comments, notes, alt text, metadata)
VISIBLE, CONCEALED, UNSEEN = "visible", "concealed", "unseen"

RULES = {
    "GW001": ("Hidden instructions for AI", "Text a human won't see contains instructions aimed at an AI."),
    "GW002": ("Concealed text", "Text is formatted to be invisible to readers but is still read by AI tools."),
    "GW003": ("AI-targeted instructions", "Visible text addresses an AI directly or tries to override its instructions."),
    "GW004": ("Hidden Unicode message", "Unicode tag characters spell out text that doesn't render."),
    "GW005": ("Invisible-character payload", "A long run of zero-width or variation-selector characters can encode data or code."),
    "GW006": ("Bidirectional control characters", "Bidi controls can make text display differently from how it's read."),
    "GW007": ("Data-leak link", "A link or image URL built to carry data out, or an instruction to send data somewhere."),
    "GW010": ("Agent can be hijacked", "An AI agent reads untrusted input, can reach private data, and can send data out."),
    "GW011": ("Agent reads untrusted input", "An AI agent is triggered by content anyone can write."),
    "GW012": ("Couldn't scan everything", "Part of the file couldn't be read, so hidden content could be missed."),
}


@dataclass
class Segment:
    text: str
    location: str                      # "page 3", "word/document.xml paragraph 12", "line 40"
    visibility: str = VISIBLE
    reason: str = ""                   # why it's hidden: "white text", "font size 0.5pt"


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    location: str
    message: str
    revealed: str = ""                 # the hidden text, shown to the user
    fix: str = ""


@dataclass
class FileReport:
    path: str
    kind: str
    findings: List[Finding] = field(default_factory=list)
    segments: int = 0
    error: Optional[str] = None

    @property
    def worst(self) -> Optional[str]:
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=lambda s: SEVERITY_ORDER[s])


# ------------------------------------------------------------------ phrasing

_AI = r"(?:a\.?i\.?|ai|artificial intelligence|llm|large language model|language model|assistant|chat ?gpt|gpt-?\d*|copilot|claude|gemini|bard|grok|bot|agent|model)"

STRONG = [
    r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,30}\b(?:previous|prior|above|earlier|all|other|your|system)\b[^.\n]{0,20}\b(?:instructions?|prompts?|rules|directions|guidelines|commands?)\b",
    rf"\b(?:attention|note|message|instructions?|important|notice)\s*(?:to|for)?\s*,?\s*(?:the\s+)?{_AI}\s*[:,!]",
    rf"\b(?:to|for)\s+(?:any|the|all)\s+{_AI}s?\b[^.\n]{{0,40}}\b(?:reading|reviewing|processing|summari[sz]ing|evaluating)\b",
    rf"\bif\s+you\s+are\s+(?:an?\s+)?{_AI}\b",
    rf"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:helpful\s+)?{_AI}\b[^.\n]{{0,40}}\b(?:must|should|will)\b",
    r"\bdo\s+not\s+(?:tell|inform|mention|reveal|disclose|show|alert)\b[^.\n]{0,40}\b(?:user|reader|human|reviewer|judge|customer|anyone)\b",
    r"\bwithout\s+(?:telling|informing|alerting|notifying)\s+(?:the\s+)?(?:user|reader|human|reviewer)\b",
    r"\b(?:give|write|provide)\s+(?:only\s+)?(?:a\s+)?(?:positive|favou?rable|glowing)\s+(?:review|assessment|evaluation|summary)\b",
    r"\bpositive\s+review\s+only\b",
    r"\b(?:rank|rate|score|recommend)\s+(?:this|the)\s+(?:candidate|applicant|resume|cv|paper|submission|proposal|bid|vendor)\b[^.\n]{0,40}\b(?:highly|highest|first|top|strongly|10)\b",
    r"\brespond\s+(?:to\s+this\s+[a-z]+\s+)?(?:superficially|briefly|positively|favou?rably)\b",
    r"\bdo\s+not\s+(?:challenge|question|dispute|contest|scrutini[sz]e)\b",
    r"<\|im_start\|>|<\|im_end\|>|\[INST\]|<<SYS>>|<\|system\|>|<\s*/?\s*system\s*>|^#{2,}\s*(?:system|instructions?)\s*:?\s*$",
    r"\b(?:new|updated|real|actual)\s+(?:system\s+)?instructions?\s*:",
    rf"\b{_AI}s?\s+(?:must|should|shall)\s+(?:always\s+|never\s+|not\s+)?(?:recommend|say|tell|mention|state|claim|avoid|ignore|rate|rank|praise|downplay)\b",
    # Portuguese and Spanish: the Brazilian court case was in Portuguese
    r"\baten[çc][ãa]o\s*,?\s*(?:intelig[êe]ncia\s+artificial|ia)\b",
    r"\bignor[ea]\s+(?:as|todas\s+as)\s+instru[çc][õo]es\b",
    r"\bresponda\s+[^.\n]{0,30}superficialmente\b",
    r"\batenci[óo]n\s*,?\s*(?:inteligencia\s+artificial|ia)\b",
    r"\bignora\s+(?:las|todas\s+las)\s+instrucciones\b",
]
SOFT = [
    rf"\bas\s+an?\s+{_AI}\b",
    rf"\b(?:the|any)\s+{_AI}\s+(?:must|should|shall|needs?\s+to)\b",
    r"\bwhen\s+(?:summari[sz]ing|reviewing|evaluating|answering)\b[^.\n]{0,40}\b(?:say|state|mention|conclude|include)\b",
    r"\b(?:include|insert|add|append)\s+(?:this|the\s+following)\s+(?:link|url|image|text|message|prompt)\b",
    r"\bcopy\s+(?:this|these)\s+(?:instructions?|text|prompt)\b",
    r"\b(?:system\s+prompt|jailbreak|developer\s+mode)\b",
]
_STRONG = [re.compile(p, re.I | re.M) for p in STRONG]
_SOFT = [re.compile(p, re.I | re.M) for p in SOFT]

_EXFIL = [
    # markdown image or link whose URL carries a placeholder to be filled in
    re.compile(r"!?\[[^\]]{0,80}\]\(\s*https?://[^)\s]*[?&][\w-]+=(?:\{[^}]*\}|\[[A-Z_ ]+\]|<[^>]+>|\$\w+|%7B[^)]*%7D)[^)]*\)", re.I),
    re.compile(r"\b(?:send|post|upload|forward|transmit|exfiltrate|leak|append)\b[^.\n]{0,60}\b(?:to|into)\s+(?:https?://|the\s+url\s+https?://)\S+", re.I),
    re.compile(r"\b(?:encode|base64)\b[^.\n]{0,60}\b(?:in(?:to)?\s+the\s+url|as\s+a\s+query|url\s+parameter)\b", re.I),
]


@dataclass
class PhraseHit:
    strength: str     # "strong" or "soft"
    text: str


def phrases(text: str) -> List[PhraseHit]:
    out = []
    for rx in _STRONG:
        m = rx.search(text)
        if m:
            out.append(PhraseHit("strong", _clip(text, m.start(), m.end())))
    if not out:
        for rx in _SOFT:
            m = rx.search(text)
            if m:
                out.append(PhraseHit("soft", _clip(text, m.start(), m.end())))
    return out


def exfil(text: str) -> Optional[str]:
    for rx in _EXFIL:
        m = rx.search(text)
        if m:
            return _clip(text, m.start(), m.end())
    return None


def _clip(text: str, a: int, b: int, pad: int = 40) -> str:
    return " ".join(text[max(0, a - pad): b + pad].split())[:200]


# ------------------------------------------------------------------ unicode

_ZW = {0x200B, 0x200C, 0x200D, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF, 0x180E}
_BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C}
_VS = set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0))


def _is_tag(cp: int) -> bool:
    return 0xE0000 <= cp <= 0xE007F


def decode_tags(s: str) -> str:
    return "".join(chr(ord(c) - 0xE0000) for c in s if 0xE0020 <= ord(c) <= 0xE007E)


@dataclass
class UnicodeHit:
    kind: str          # "tags", "payload", "bidi"
    count: int
    decoded: str = ""


def unicode_tricks(text: str) -> List[UnicodeHit]:
    out = []
    tags = [c for c in text if _is_tag(ord(c))]
    if tags:
        # A tag sequence right after a black flag is a legitimate subdivision flag (🏴󠁧󠁢󠁳󠁣󠁴󠁿)
        stripped = re.sub("\U0001F3F4[\U000E0020-\U000E007F]+\U000E007F", "", text)
        tags = [c for c in stripped if _is_tag(ord(c))]
        if tags:
            out.append(UnicodeHit("tags", len(tags), decode_tags(stripped)))
    # long runs of invisible characters, which is how Glassworm hid its code
    best = 0
    run = 0
    prev_visible = ""
    for c in text:
        cp = ord(c)
        if cp in _ZW or (cp in _VS and not _emoji_base(prev_visible)):
            run += 1
            best = max(best, run)
        else:
            run = 0
            if not (cp in _VS):
                prev_visible = c
    if best >= 8:
        out.append(UnicodeHit("payload", best))
    bidi = sum(1 for c in text if ord(c) in _BIDI and ord(c) not in (0x200E, 0x200F))
    if bidi and not _mostly_rtl(text):
        out.append(UnicodeHit("bidi", bidi))
    return out


def _emoji_base(c: str) -> bool:
    if not c:
        return False
    cp = ord(c)
    return cp >= 0x2190 and not (0x3000 <= cp <= 0x9FFF)  # symbols and emoji take variation selectors


def _mostly_rtl(text: str) -> bool:
    rtl = sum(1 for c in text if "\u0590" <= c <= "\u08ff" or "\ufb1d" <= c <= "\ufefc")
    return rtl > 20


def strip_invisible(s: str) -> str:
    return "".join(c for c in s if ord(c) not in _ZW and ord(c) not in _BIDI and ord(c) not in _VS and not _is_tag(ord(c)))


# ------------------------------------------------------------------ judging

def _substantive(text: str) -> bool:
    letters = sum(1 for c in text if c.isalpha())
    return letters >= 12 and len(text.split()) >= 3


def _sentence_sized(text: str) -> bool:
    """Short hidden labels ("Bottom of Form") are common; hidden prose isn't."""
    letters = sum(1 for c in text if c.isalpha())
    return letters >= 30 and len(text.split()) >= 6


MAX_SEGMENT = 200_000
MAX_SEGMENTS = 200_000


def judge(path: str, segments: Iterable[Segment]) -> List[Finding]:
    out: List[Finding] = []
    seen = set()
    judged = set()   # identical text in the same situation is judged once (a 1 MB string in 1,000 cells)

    def add(f: Finding):
        key = (f.rule, f.location, f.message, f.revealed)
        if key not in seen:
            seen.add(key)
            out.append(f)

    for n, seg in enumerate(segments):
        if n >= MAX_SEGMENTS:
            break
        text = seg.text[:MAX_SEGMENT]
        if not text or not text.strip():
            continue
        key = (seg.visibility, seg.reason, hash(text))
        if key in judged:
            continue
        judged.add(key)

        for u in unicode_tricks(text):
            if u.kind == "tags":
                add(Finding("GW004", HIGH, path, seg.location,
                            f"{u.count} invisible Unicode tag characters spell out a hidden message",
                            u.decoded[:300], "Delete the hidden characters. If you didn't put them there, treat the "
                                             "file as hostile."))
            elif u.kind == "payload":
                add(Finding("GW005", HIGH if u.count >= 64 else MEDIUM, path, seg.location,
                            f"a run of {u.count} invisible characters that can encode data or code", "",
                            "Glassworm used this to hide malware in plain sight. Strip the characters and review."))
            elif u.kind == "bidi":
                add(Finding("GW006", MEDIUM, path, seg.location,
                            f"{u.count} bidirectional control character(s) in mostly left-to-right text", "",
                            "Check that the text reads the same way it displays."))

        clean = strip_invisible(text)
        hits = phrases(clean)
        leak = exfil(clean)
        strong = any(h.strength == "strong" for h in hits)

        if seg.visibility == CONCEALED and _substantive(clean):
            if hits or leak:
                add(Finding("GW001", HIGH, path, seg.location,
                            f"concealed text ({seg.reason}) gives instructions to an AI", clean[:400],
                            "Remove the hidden text before any AI reads this file, and find out who added it."))
            else:
                add(Finding("GW002", MEDIUM if _sentence_sized(clean) else LOW, path, seg.location,
                            f"text is concealed from readers ({seg.reason}), but AI tools will read it", clean[:400],
                            "Make it visible or delete it. Hidden text is how instructions get smuggled to AI."))
            if leak:
                add(Finding("GW007", HIGH, path, seg.location, "concealed text tries to send data out", leak))
            continue

        if seg.visibility == UNSEEN and (hits or leak):
            add(Finding("GW001", HIGH if (strong or leak) else MEDIUM, path, seg.location,
                        f"{seg.reason or 'text people rarely look at'} gives instructions to an AI",
                        clean[:400], "Remove it before any AI reads this file."))
            if leak:
                add(Finding("GW007", HIGH, path, seg.location, "hidden content tries to send data out", leak))
            continue

        if seg.visibility == VISIBLE:
            if leak:
                add(Finding("GW007", HIGH, path, seg.location, "content tries to send data out through a link", leak,
                            "AI tools that render images or follow links can leak data this way."))
            if hits:
                add(Finding("GW003", MEDIUM if strong else LOW, path, seg.location,
                            "text speaks directly to an AI", hits[0].text,
                            "Fine in an article about AI; suspicious in a CV, contract, ticket or web page."))
    return out
