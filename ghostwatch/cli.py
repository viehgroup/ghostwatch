import argparse
import os
import sys
from typing import List

from . import __version__, report
from .agents import audit_repo
from .core import LOW, MEDIUM, RULES, SEVERITY_ORDER, FileReport, judge
from .github import GitHubError, from_event_file, from_issue
from .scan import scan_file, walk

COMMANDS = {"scan", "issue", "event", "agents", "rules"}


def build_parser():
    p = argparse.ArgumentParser(
        prog="ghostwatch",
        description="Find text that people can't see but AI can: hidden instructions in documents, web pages, "
                    "GitHub issues, and risky AI agent workflows.",
        epilog="Shortcut: `ghostwatch FILE...` is the same as `ghostwatch scan FILE...`.",
    )
    p.add_argument("--version", action="version", version=f"ghostwatch {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=["text", "json", "sarif", "github", "markdown"], default="text")
    common.add_argument("-o", "--output", help="write the report to a file")
    common.add_argument("--summary", metavar="FILE", help="also append a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    common.add_argument("--fail-on", choices=["high", "medium", "low", "never"], default="high")
    common.add_argument("--ignore", action="append", default=[], metavar="RULE")
    common.add_argument("--all", action="store_true", help="also show low-severity findings")
    common.add_argument("--no-color", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", parents=[common], help="scan files and folders (default: current folder)")
    s.add_argument("paths", nargs="*", default=["."])
    s.add_argument("--no-agents", action="store_true", help="skip the AI agent workflow audit for folders")
    i = sub.add_parser("issue", parents=[common], help="scan a GitHub issue or PR with its comments")
    i.add_argument("ref", help="owner/repo#123 or an issue/PR URL")
    e = sub.add_parser("event", parents=[common], help="scan the issue, PR or comment that triggered a GitHub Action")
    e.add_argument("--file", help="event JSON (default: $GITHUB_EVENT_PATH)")
    a = sub.add_parser("agents", parents=[common], help="audit AI agent workflows in a repository")
    a.add_argument("repo", nargs="?", default=".")
    sub.add_parser("rules", help="list all rules")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "scan")
    try:
        return _main(argv)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 130


def _main(argv: List[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "rules":
        for rid, (title, desc) in RULES.items():
            print(f"{rid}  {title}\n       {desc}")
        return 0

    reports: List[FileReport] = []
    scanned = 0
    try:
        if args.cmd == "scan":
            for path in args.paths:
                if not os.path.exists(path):
                    print(f"ghostwatch: {path}: no such file or folder", file=sys.stderr)
                    return 2
            for path in walk(args.paths):
                scanned += 1
                rep = scan_file(path, os.path.relpath(path) if not os.path.isabs(path) else path)
                if rep.findings:
                    reports.append(rep)
            if not args.no_agents:
                for path in args.paths:
                    if os.path.isdir(path):
                        found = audit_repo(path)
                        if found:
                            reports.append(FileReport(".github/workflows", "workflow", found))
        elif args.cmd == "agents":
            found = audit_repo(args.repo)
            scanned = 1
            reports.append(FileReport(".github/workflows", "workflow", found))
        else:
            label, segs = from_event_file(args.file) if args.cmd == "event" else from_issue(args.ref)
            scanned = 1
            reports.append(FileReport(label, "github", judge(label, segs), segments=len(segs)))
    except GitHubError as e:
        print(f"ghostwatch: {e}", file=sys.stderr)
        return 2

    ignore = {r.upper() for r in args.ignore}
    for r in reports:
        r.findings = [f for f in r.findings if f.rule not in ignore]
    min_sev = LOW if args.all else MEDIUM

    if args.format == "json":
        out = report.as_json(reports)
    elif args.format == "sarif":
        out = report.sarif(reports)
    elif args.format == "github":
        out = report.github(reports)
    elif args.format == "markdown":
        out = report.markdown(reports, min_sev)
    else:
        color = not args.output and sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
        out = report.text(reports, color, min_sev, scanned)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    elif out:
        print(out)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(report.markdown(reports, min_sev) + "\n")

    if args.fail_on == "never":
        return 0
    t = SEVERITY_ORDER[args.fail_on]
    return 1 if any(SEVERITY_ORDER[f.severity] >= t for r in reports for f in r.findings) else 0
