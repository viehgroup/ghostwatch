import json
import shutil
import textwrap
from typing import List

from . import __version__
from .core import HIGH, LOW, MEDIUM, RULES, SEVERITY_ORDER, FileReport

_C = {HIGH: "31", MEDIUM: "33", LOW: "36", "dim": "2", "bold": "1", "ok": "32", "reveal": "35"}
_ICON = {HIGH: "🔴", MEDIUM: "🟠", LOW: "🔵"}


def _p(on: bool):
    return lambda s, k: f"\033[{_C[k]}m{s}\033[0m" if on else s


def _visible(reports: List[FileReport], min_sev: str):
    t = SEVERITY_ORDER[min_sev]
    for r in reports:
        items = [f for f in r.findings if SEVERITY_ORDER[f.severity] >= t]
        if items:
            yield r, sorted(items, key=lambda f: -SEVERITY_ORDER[f.severity])


def _counts(reports):
    c = {HIGH: 0, MEDIUM: 0, LOW: 0}
    for r in reports:
        for f in r.findings:
            c[f.severity] += 1
    return c


def text(reports: List[FileReport], color: bool, min_sev: str = MEDIUM, scanned: int = 0) -> str:
    p = _p(color)
    width = min(100, shutil.get_terminal_size((80, 24)).columns)
    out = []
    for r, items in _visible(reports, min_sev):
        out.append(p(r.path, "bold") + p(f"  ({r.kind})", "dim"))
        for f in items:
            head = f"  {p(f'{f.severity:<6}', f.severity)} {f.rule}  {RULES[f.rule][0]}"
            plain_len = 2 + 7 + len(f.rule) + 2 + len(RULES[f.rule][0])
            if f.location and plain_len + len(f.location) + 4 <= width:
                head += p(f"  [{f.location}]", "dim")
                out.append(head)
            else:
                out.append(head)
                if f.location:
                    out.append("    " + p(f"[{f.location}]", "dim"))
            for line in textwrap.wrap(f.message, width - 4, break_on_hyphens=False):
                out.append("    " + line)
            if f.revealed:
                out.append("    " + p("AI reads:", "reveal"))
                shown = f.revealed if len(f.revealed) <= 600 else f.revealed[:600] + " …"
                for line in textwrap.wrap(shown, width - 8, break_on_hyphens=False):
                    out.append("    " + p("│ ", "reveal") + line)
            if f.fix:
                for i, line in enumerate(textwrap.wrap(f.fix, width - 9, break_on_hyphens=False)):
                    out.append(("    fix: " if i == 0 else "         ") + p(line, "dim"))
        out.append("")
    c = _counts(reports)
    hidden = sum(1 for r in reports for f in r.findings if SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[min_sev])
    parts = [f"{n} {s}" for s, n in c.items() if n]
    summary = f"{scanned or len(reports)} item{'s' if (scanned or len(reports)) != 1 else ''} scanned"
    if parts:
        summary += ": " + ", ".join(parts)
        if hidden and min_sev != LOW:
            summary += p(f" ({hidden} low not shown; use --all)", "dim")
    else:
        summary += ". " + p("No hidden text or AI instructions found.", "ok")
    out.append(summary)
    return "\n".join(out)


def markdown(reports: List[FileReport], min_sev: str = MEDIUM) -> str:
    c = _counts(reports)
    rows = list(_visible(reports, min_sev))
    lines = ["## ghostwatch"]
    if not rows:
        lines.append("✅ No hidden text or instructions for AI found.")
        return "\n".join(lines) + "\n"
    lines.append(", ".join(f"{_ICON[s]} {n} {s}" for s, n in c.items() if n and SEVERITY_ORDER[s] >= SEVERITY_ORDER[min_sev]))
    if c[HIGH]:
        lines.append("\n> **Don't let an AI agent act on this content** until the high-severity items are explained.")
    for r, items in rows:
        lines.append(f"\n### `{r.path}`")
        for f in items:
            lines.append(f"- {_ICON[f.severity]} **{RULES[f.rule][0]}** ({f.rule}{', ' + f.location if f.location else ''}): {f.message}")
            if f.revealed:
                safe = f.revealed[:500].replace("`", "'")
                lines.append(f"  <details><summary>What an AI reads</summary>\n\n  ```text\n  {safe}\n  ```\n  </details>")
    lines.append(f"\n<sub>ghostwatch {__version__} · see what your AI sees</sub>")
    return "\n".join(lines) + "\n"


def as_json(reports: List[FileReport]) -> str:
    return json.dumps({"version": __version__, "files": [{
        "path": r.path, "kind": r.kind, "error": r.error,
        "findings": [f.__dict__ for f in r.findings]} for r in reports]}, indent=2, ensure_ascii=False)


def sarif(reports: List[FileReport]) -> str:
    level = {HIGH: "error", MEDIUM: "warning", LOW: "note"}
    findings = [f for r in reports for f in r.findings]
    used = sorted({f.rule for f in findings})
    return json.dumps({
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "ghostwatch", "version": __version__,
                "informationUri": "https://github.com/viehgroup/ghostwatch",
                "rules": [{"id": r, "name": RULES[r][0].replace(" ", ""),
                           "shortDescription": {"text": RULES[r][0]},
                           "fullDescription": {"text": RULES[r][1]}} for r in used],
            }},
            "results": [{
                "ruleId": f.rule, "level": level[f.severity],
                "message": {"text": f"{f.message}" + (f" Hidden text: {f.revealed[:300]}" if f.revealed else "")},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.file}}}],
            } for f in findings],
        }],
    }, indent=2, ensure_ascii=False)


def github(reports: List[FileReport]) -> str:
    kind = {HIGH: "error", MEDIUM: "warning", LOW: "notice"}

    def esc(s):
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    def prop(s):
        return esc(s).replace(":", "%3A").replace(",", "%2C")

    lines = []
    for r in reports:
        for f in r.findings:
            msg = f.message + (f" | Hidden text: {f.revealed[:200]}" if f.revealed else "")
            file_part = f"file={prop(f.file)}," if not f.file.startswith(("issue", "pull", "discussion", "event")) else ""
            lines.append(f"::{kind[f.severity]} {file_part}title={prop(f.rule + ' ' + RULES[f.rule][0])}::{esc(msg)}")
    return "\n".join(lines)
