"""Text from GitHub issues, pull requests and comments: what AI agents read first.

GitLost (2026) started with an issue anyone could open. Scanning that
text before an agent sees it is the cheapest defence there is.
"""

import json
import os
import re
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from . import __version__
from .core import Segment
from .formats.web import read_markdown

API = "https://api.github.com"


class GitHubError(Exception):
    pass


def _md(text: Optional[str], where: str) -> List[Segment]:
    if not text:
        return []
    return [Segment(s.text, f"{where} {s.location}", s.visibility, s.reason) for s in read_markdown(text)]


def from_event(payload: dict) -> Tuple[str, List[Segment]]:
    """Segments from a GitHub Actions event payload (GITHUB_EVENT_PATH)."""
    segs: List[Segment] = []
    label = "event"
    for kind in ("issue", "pull_request", "discussion"):
        obj = payload.get(kind)
        if isinstance(obj, dict):
            num = obj.get("number", "?")
            label = f"{kind.replace('_', ' ')} #{num}"
            segs += _md(obj.get("title"), f"{label} title")
            segs += _md(obj.get("body"), f"{label} body")
    for kind in ("comment", "review"):
        obj = payload.get(kind)
        if isinstance(obj, dict) and obj.get("body"):
            who = (obj.get("user") or {}).get("login", "someone")
            segs += _md(obj["body"], f"{kind} by @{who}")
    return label, segs


def from_event_file(path: Optional[str] = None) -> Tuple[str, List[Segment]]:
    path = path or os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise GitHubError("no event file (GITHUB_EVENT_PATH isn't set)")
    try:
        with open(path, encoding="utf-8") as fh:
            return from_event(json.load(fh))
    except (OSError, ValueError) as e:
        raise GitHubError(f"can't read {path}: {e}") from e


def _get(url: str, token: Optional[str]):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"ghostwatch/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise GitHubError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise GitHubError(f"{url}: {e}") from e


def parse_ref(ref: str) -> Tuple[str, int]:
    """owner/repo#123 or a github.com issue/PR URL."""
    m = re.match(r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/(?:issues|pull|discussions)/)(\d+)", ref.strip())
    if not m:
        raise GitHubError(f"expected owner/repo#123 or an issue URL, got {ref!r}")
    return m.group(1), int(m.group(2))


def from_issue(ref: str, token: Optional[str] = None) -> Tuple[str, List[Segment]]:
    """An issue or pull request with all its comments, fetched from the API."""
    repo, num = parse_ref(ref)
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    issue = _get(f"{API}/repos/{repo}/issues/{num}", token)
    label = f"{repo}#{num}"
    segs = _md(issue.get("title"), "title") + _md(issue.get("body"), "body")
    page = 1
    while page <= 10:
        comments = _get(f"{API}/repos/{repo}/issues/{num}/comments?per_page=100&page={page}", token)
        if not comments:
            break
        for c in comments:
            segs += _md(c.get("body"), f"comment by @{(c.get('user') or {}).get('login', 'someone')}")
        if len(comments) < 100:
            break
        page += 1
    return label, segs
