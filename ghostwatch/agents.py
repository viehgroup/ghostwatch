"""Audit GitHub workflows that run AI agents, for the GitLost pattern.

An agent can be hijacked by hidden instructions when three things line up:
  1. anyone can trigger it with text they write (issues, comments, fork PRs),
  2. it can reach something sensitive (extra tokens, write access, cloud credentials),
  3. it can send something back out (comments, PRs, network).

Defaults matter and differ per tool, so they're modelled explicitly:
  * anthropics/claude-code-action only runs for users with write access unless
    `allowed_non_write_users` is set.
  * gh-aw only runs for admin, maintainer and write roles unless `roles` includes
    `all` or `read`.
  * For other agents we don't assume a built-in check: an `if:` that tests
    author_association or the actor counts as the gate.

This reads workflow text with simple rules, not a YAML parser, so it can miss
unusual layouts. It's a review aid, not a proof of safety.
"""

import os
import re
from typing import List, Optional

from .core import HIGH, MEDIUM, Finding

UNTRUSTED = ("issues", "issue_comment", "pull_request_target", "discussion", "discussion_comment",
             "pull_request_review", "pull_request_review_comment")
CLAUDE = re.compile(r"uses:\s*['\"]?anthropics/claude-code(?:-base)?-action\b")
OTHER_AGENTS = re.compile(r"uses:\s*['\"]?(?:google-github-actions/run-gemini-cli|openai/codex-action|"
                          r"github/copilot[\w-]*|coderabbitai/[\w-]+|cursor/[\w-]+)\b"
                          r"|^\s*(?:-\s*)?run:.*\b(?:claude\s+(?:-p|--print)|codex\s+exec|gemini\s+(?:-p|--prompt)|aider\s)",
                          re.M)
# A gate has to compare something; merely mentioning author_association (say, in a debug variable) isn't one
GATE = re.compile(r"(?:contains\(|[!=]=)[^\n]{0,200}author_association|author_association\s*[!=]="
                  r"|github\.(?:triggering_)?actor\s*[!=]=|\.user\.login\s*[!=]="
                  r"|permission-check|has-write-access|collaborators/[^\n]*permission", re.I)
MODEL_SECRETS = re.compile(r"^(?:ANTHROPIC|OPENAI|GEMINI|GOOGLE|AZURE_OPENAI|MISTRAL|GROQ|OPENROUTER)_?API_?KEY$|"
                           r"^CLAUDE_CODE_OAUTH_TOKEN$|^COPILOT_\w*TOKEN$|^GITHUB_TOKEN$")
WRITE_PERMS = re.compile(r"^\s*(?:permissions:\s*write-all|contents:\s*write|id-token:\s*write|actions:\s*write|"
                         r"packages:\s*write|deployments:\s*write|security-events:\s*write)\s*(?:#.*)?$", re.M)


def _frontmatter(text: str) -> str:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    return m.group(1) if m else ""


def triggers(text: str) -> List[str]:
    """Event names from the `on:` block, in any of its three YAML shapes."""
    m = re.search(r"^(['\"]?on['\"]?|true)[ \t]*:[ \t]*(.*)$", text, re.M)
    if not m:
        return []
    inline = m.group(2).split("#")[0].strip()
    if inline:
        return [t.strip(" '\"") for t in inline.strip("[]").split(",") if t.strip(" '\"")]
    out = []
    base = None
    for line in text[m.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if base is None:
            base = indent
        if indent < base or indent == 0:
            break
        if indent == base:
            k = re.match(r"\s*(?:-\s*)?['\"]?([\w-]+)['\"]?\s*:?", line)
            if k:
                out.append(k.group(1))
    return out


def _secrets(text: str) -> List[str]:
    names = sorted(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)))
    return [n for n in names if not MODEL_SECRETS.match(n.upper())]


def audit_text(path: str, text: str) -> List[Finding]:
    is_aw = path.endswith(".md")
    head = _frontmatter(text) if is_aw else text
    if is_aw and not head:
        return []
    claude = bool(CLAUDE.search(text))
    other = bool(OTHER_AGENTS.search(text))
    aw = is_aw and bool(re.search(r"^(?:engine|safe-outputs)\s*:", head, re.M))
    if not (claude or other or aw):
        return []

    trig = [t for t in triggers(head) if t in UNTRUSTED]
    if not trig:
        return []

    # Who can actually set it off?
    why_open: Optional[str] = None
    if claude:
        m = re.search(r"allowed_non_write_users\s*:\s*['\"]?([^'\"\n#]+)", text)
        if m:
            who = m.group(1).strip()
            why_open = "allowed_non_write_users is set" + (" to everyone (\"*\")" if who == "*" else f" ({who})")
    elif aw:
        m = re.search(r"^\s*roles\s*:\s*\[?([^\]\n#]*)", head, re.M)
        if m and re.search(r"\b(?:all|read)\b", m.group(1)):
            why_open = f"roles: {m.group(1).strip()} lets any user trigger it"
    elif not GATE.search(text):
        why_open = "nothing checks who triggered it (no author_association or actor check)"
    if not why_open:
        return []

    reach = []
    extra = _secrets(text)
    if extra:
        reach.append("secrets " + ", ".join(extra[:4]))
    perms = sorted({p.strip().split("#")[0].strip() for p in WRITE_PERMS.findall(head)})
    if perms:
        reach.append(", ".join(perms))
    if "pull_request_target" in trig:
        reach.append("pull_request_target, which runs with the base repository's secrets")

    loc = ", ".join(trig)
    if reach:
        return [Finding("GW010", HIGH, path, f"on: {loc}",
                        f"an AI agent anyone can trigger ({why_open}) can reach {'; '.join(reach)} and post results. "
                        "Hidden instructions in an issue or comment could steer it, as in GitLost.",
                        fix="Limit who can trigger it, drop secrets and write permissions it doesn't need, and scan "
                            "untrusted text first (ghostwatch's action can run before the agent).")]
    return [Finding("GW011", MEDIUM, path, f"on: {loc}",
                    f"an AI agent reads text anyone can write ({why_open})",
                    fix="That's fine for triage bots if it can't reach anything sensitive. Keep permissions minimal "
                        "and scan the text before the agent runs.")]


def audit_repo(root: str) -> List[Finding]:
    wf = os.path.join(root, ".github", "workflows")
    out: List[Finding] = []
    if not os.path.isdir(wf):
        return out
    for fn in sorted(os.listdir(wf)):
        if not fn.endswith((".yml", ".yaml", ".md")):
            continue
        p = os.path.join(wf, fn)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read(2_000_000)
        except OSError:
            continue
        out += audit_text(os.path.relpath(p, root).replace(os.sep, "/"), text)
    return out
