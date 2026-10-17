#!/usr/bin/env python3
"""gitignore-check: parse, explain, and audit .gitignore rules the way git applies them.

A faithful, dependency-free Python implementation of git's exclusion engine:

- Pattern parsing mirrors git's dir.c (add_patterns_from_buffer,
  trim_trailing_spaces, parse_path_pattern): comment and blank-line handling,
  CRLF and BOM handling, unescaped trailing-space stripping, '!' negation,
  trailing-'/' directory-only patterns, anchoring by any leading/middle '/'.
- Matching is a port of git's wildmatch.c (dowild): '*' and '?' never match
  '/' in pathname mode, '**' crosses directories only in the documented
  positions, character classes support ranges, negation and POSIX classes.
- Precedence mirrors dir.c (last_matching_pattern_from_list): rules from a
  deeper .gitignore win over shallower ones; within a file, the LAST matching
  line wins. A negated match means "not ignored" and stops the scan.
- The parent-directory rule from gitignore(5): a path under an excluded
  directory can never be re-included, no matter what later rules say.

Nothing here shells out to git and nothing touches the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# wildmatch port (git wildmatch.c, function dowild). Case-sensitive only:
# git passes WM_CASEFOLD just when core.ignoreCase is set, which this tool
# does not read. Return codes match the C original.
# --------------------------------------------------------------------------

WM_MATCH = 1
WM_NOMATCH = 0
WM_ABORT_ALL = -1
WM_ABORT_TO_STARSTAR = -2

_GLOB_SPECIAL = frozenset("*?[\\")


def _posix_classes() -> Dict[str, Callable[[str], bool]]:
    def in_set(chars: str) -> Callable[[str], bool]:
        return lambda ch: ch != "" and ch in chars

    def in_range(lo: int, hi: int) -> Callable[[str], bool]:
        return lambda ch: ch != "" and lo <= ord(ch) <= hi

    digit = in_set("0123456789")
    upper = in_range(ord("A"), ord("Z"))
    lower = in_range(ord("a"), ord("z"))
    alpha = lambda ch: upper(ch) or lower(ch)
    alnum = lambda ch: alpha(ch) or digit(ch)
    graph = in_range(33, 126)
    return {
        "alnum": alnum,
        "alpha": alpha,
        "blank": in_set(" \t"),
        "cntrl": lambda ch: ch != "" and (ord(ch) < 32 or ord(ch) == 127),
        "digit": digit,
        "graph": graph,
        "lower": lower,
        "print": in_range(32, 126),
        "punct": lambda ch: graph(ch) and not alnum(ch),
        "space": in_set(" \t\n\v\f\r"),
        "upper": upper,
        "xdigit": in_set("0123456789abcdefABCDEF"),
    }


_POSIX = _posix_classes()


def _dowild(pat: str, text: str, pathname: bool) -> int:
    """Port of dowild() from git's wildmatch.c (pathname == WM_PATHNAME)."""
    p = 0
    t = 0
    while p < len(pat):
        p_ch = pat[p]
        t_ch = text[t] if t < len(text) else ""
        if t_ch == "" and p_ch != "*":
            return WM_ABORT_ALL
        if p_ch == "\\":
            # Literal match with following character; a trailing backslash
            # makes p_ch "", which no real character equals -> never matches.
            p += 1
            p_ch = pat[p] if p < len(pat) else ""
            if t_ch != p_ch:
                return WM_NOMATCH
            p += 1
            t += 1
            continue
        if p_ch == "?":
            if pathname and t_ch == "/":
                return WM_NOMATCH
            p += 1
            t += 1
            continue
        if p_ch == "[":
            p += 1
            p_ch = pat[p] if p < len(pat) else ""
            if p_ch == "^":  # NEGATE_CLASS2
                p_ch = "!"
            negated = p_ch == "!"
            if negated:
                p += 1
                p_ch = pat[p] if p < len(pat) else ""
            prev_ch = ""
            matched = False
            # do { ... } while (p_ch != ']')
            while True:
                if p_ch == "":
                    return WM_ABORT_ALL
                if p_ch == "\\":
                    p += 1
                    p_ch = pat[p] if p < len(pat) else ""
                    if p_ch == "":
                        return WM_ABORT_ALL
                    if t_ch == p_ch:
                        matched = True
                elif (
                    p_ch == "-"
                    and prev_ch != ""
                    and p + 1 < len(pat)
                    and pat[p + 1] != "]"
                ):
                    p += 1
                    p_ch = pat[p]
                    if p_ch == "\\":
                        p += 1
                        p_ch = pat[p] if p < len(pat) else ""
                        if p_ch == "":
                            return WM_ABORT_ALL
                    if prev_ch <= t_ch <= p_ch:
                        matched = True
                    p_ch = ""
                elif p_ch == "[" and p + 1 < len(pat) and pat[p + 1] == ":":
                    s = p + 2
                    q = s
                    while q < len(pat) and pat[q] != "]":
                        q += 1
                    if q >= len(pat):
                        return WM_ABORT_ALL
                    if q - s < 1 or pat[q - 1] != ":":
                        # No ":]" found: treat like a normal set whose first
                        # member is a literal '['.
                        p = s - 2
                        p_ch = "["
                        if t_ch == p_ch:
                            matched = True
                    else:
                        cls = _POSIX.get(pat[s : q - 1])
                        if cls is None:
                            return WM_ABORT_ALL  # malformed [:class:]
                        if cls(t_ch):
                            matched = True
                        p = q
                        p_ch = ""
                elif t_ch == p_ch:
                    matched = True
                prev_ch = p_ch
                p += 1
                p_ch = pat[p] if p < len(pat) else ""
                if p_ch == "]":
                    break
            if matched == negated or (pathname and t_ch == "/"):
                return WM_NOMATCH
            p += 1
            t += 1
            continue
        if p_ch != "*":
            # Literal character (the "default" switch case in dowild).
            if t_ch != p_ch:
                return WM_NOMATCH
            p += 1
            t += 1
            continue
        # p_ch == '*'
        p += 1
        if p < len(pat) and pat[p] == "*":
            prev_p = p  # index of the second '*'
            while p < len(pat) and pat[p] == "*":
                p += 1
            if not pathname:
                match_slash = True
            elif (prev_p < 2 or pat[prev_p - 2] == "/") and (
                p >= len(pat)
                or pat[p] == "/"
                or (pat[p] == "\\" and p + 1 < len(pat) and pat[p + 1] == "/")
            ):
                # '**' at the start or after '/', followed by end or '/'.
                if p < len(pat) and pat[p] == "/":
                    # Try matching the rest against the current text: this
                    # makes 'foo/**/bar' match 'foo/bar' (zero directories).
                    if _dowild(pat[p + 1 :], text[t:], pathname) == WM_MATCH:
                        return WM_MATCH
                match_slash = True
            else:
                match_slash = False
        else:
            match_slash = not pathname
        if p >= len(pat):
            # Trailing "**" matches everything; trailing "*" matches only if
            # no slash remains in pathname mode.
            if not match_slash and "/" in text[t:]:
                return WM_ABORT_TO_STARSTAR
            return WM_MATCH
        if not match_slash and pat[p] == "/":
            # A single '*' followed by '/' matches the next directory.
            slash = text.find("/", t)
            if slash == -1:
                return WM_ABORT_ALL
            t = slash
            p += 1
            t += 1
            continue
        while True:
            t_ch = text[t] if t < len(text) else ""
            if t_ch == "":
                break
            if pat[p] not in _GLOB_SPECIAL:
                # Literal next char: skip ahead to it (never past '/' unless
                # this star may match slashes).
                lit = pat[p]
                while t < len(text) and (match_slash or text[t] != "/"):
                    if text[t] == lit:
                        break
                    t += 1
                t_ch = text[t] if t < len(text) else ""
                if t_ch != lit:
                    return WM_ABORT_ALL if match_slash else WM_ABORT_TO_STARSTAR
            matched = _dowild(pat[p:], text[t:], pathname)
            if matched != WM_NOMATCH:
                if not match_slash or matched != WM_ABORT_TO_STARSTAR:
                    return matched
            elif not match_slash and t_ch == "/":
                return WM_ABORT_TO_STARSTAR
            t += 1
        return WM_ABORT_ALL
    return WM_NOMATCH if t < len(text) else WM_MATCH


def wildmatch(pattern: str, text: str, pathname: bool = False) -> bool:
    """True if git's wildmatch matches. pathname=True is WM_PATHNAME mode:
    '*' and '?' do not cross '/' and '**' has its special meaning."""
    return _dowild(pattern, text, pathname) == WM_MATCH


# --------------------------------------------------------------------------
# Pattern parsing (git dir.c: add_patterns_from_buffer, trim_trailing_spaces,
# parse_path_pattern).
# --------------------------------------------------------------------------


def _trim_trailing_spaces(line: str) -> str:
    """Port of trim_trailing_spaces(): strip the trailing run of spaces
    unless it is quoted with a backslash. Only ' ' is stripped, never tabs.
    A trailing backslash aborts the trim entirely (as in the C original)."""
    last_space: Optional[int] = None
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            if last_space is None:
                last_space = i
        elif c == "\\":
            i += 1
            if i >= len(line):
                return line
            last_space = None
        else:
            last_space = None
        i += 1
    return line if last_space is None else line[:last_space]


@dataclass(frozen=True)
class Rule:
    pattern: str  # ready for wildmatch (leading '/' removed, escapes kept)
    negated: bool
    dir_only: bool
    anchored: bool  # any '/' in the pattern after removing a trailing one
    raw: str  # the line as written, before trailing-space trimming
    had_trailing_spaces: bool
    lineno: int
    source: str  # display name of the file, e.g. ".gitignore" or "doc/.gitignore"
    base: str  # directory of the .gitignore relative to the root ('' = root)

    def matches(self, path: str, is_dir: bool) -> bool:
        """path: relative to the root, '/'-separated, no leading slash."""
        if self.base:
            if not path.startswith(self.base + "/"):
                return False
            rel = path[len(self.base) + 1 :]
        else:
            rel = path
        if not rel:
            return False
        if self.dir_only and not is_dir:
            return False
        if self.anchored:
            return wildmatch(self.pattern, rel, pathname=True)
        basename = rel.rsplit("/", 1)[-1]
        return wildmatch(self.pattern, basename, pathname=False)

    def describe(self) -> str:
        return f"{self.source}:{self.lineno}: {self.raw}"


def parse_lines(text: str, source: str = "<string>", base: str = "") -> List[Rule]:
    """Parse gitignore file content into rules. Mirrors git's buffer parser:
    UTF-8 BOM skipped, CRLF tolerated, blank lines and '#' comments skipped,
    unescaped trailing spaces stripped. '\\!' and '\\#' stay escaped in the
    pattern and are unescaped by wildmatch, exactly like git."""
    rules: List[Rule] = []
    if text.startswith("\ufeff"):
        text = text[1:]
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line == "" or line[0] == "#":
            continue
        trimmed = _trim_trailing_spaces(line)
        if trimmed == "":
            continue
        s = trimmed
        negated = False
        if s.startswith("!"):
            negated = True
            s = s[1:]
        dir_only = False
        if s.endswith("/"):
            dir_only = True
            s = s[:-1]
        anchored = "/" in s
        if s.startswith("/"):
            s = s[1:]
        rules.append(
            Rule(
                pattern=s,
                negated=negated,
                dir_only=dir_only,
                anchored=anchored,
                raw=trimmed,
                had_trailing_spaces=trimmed != line,
                lineno=lineno,
                source=source,
                base=base,
            )
        )
    return rules


# --------------------------------------------------------------------------
# Decision engine (git dir.c: last_matching_pattern_from_list(s), is_excluded)
# plus the parent-directory rule from gitignore(5).
# --------------------------------------------------------------------------


@dataclass
class Decision:
    path: str
    is_dir: bool
    ignored: bool
    rule: Optional[Rule]  # the rule that decided, or None if not ignored
    forced_parent: Optional[str]  # set when an excluded parent dir decided
    chain: List[Rule] = field(default_factory=list)  # all rules matching path

    def explain_lines(self) -> List[str]:
        kind = "directory" if self.is_dir else "file"
        out = [f"path: {self.path} ({kind})"]
        out.append(f"verdict: {'IGNORED' if self.ignored else 'NOT IGNORED'}")
        if self.rule is not None:
            out.append(f"deciding rule: {self.rule.describe()}")
        else:
            out.append("no rule matches")
        if self.forced_parent is not None:
            out.append(
                f'note: parent directory "{self.forced_parent}" is excluded, so no '
                "rule matching this path can re-include it (gitignore(5))"
            )
        if self.chain:
            if self.forced_parent is not None:
                out.append("rules that match the path itself (none take effect):")
            else:
                out.append("matching rules in evaluation order (first decides):")
            for r in self.chain:
                tag = "negation -> include" if r.negated else "exclude"
                out.append(f"  {r.describe()}  [{tag}]")
        return out


class IgnoreSet:
    """Rules from one or more .gitignore files, keyed by their base dir."""

    def __init__(self) -> None:
        self.rules_by_base: Dict[str, List[Rule]] = {}

    def add_rules(self, base: str, rules: List[Rule]) -> None:
        self.rules_by_base.setdefault(base, []).extend(rules)

    def add_file_text(self, text: str, source: str, base: str) -> None:
        self.add_rules(base, parse_lines(text, source=source, base=base))

    def _applicable_bases(self, path: str) -> List[str]:
        """Base dirs whose rules apply to path, deepest first."""
        bases = [""]
        parts = path.split("/")
        for i in range(1, len(parts)):
            bases.append("/".join(parts[:i]))
        return [b for b in reversed(bases) if b in self.rules_by_base]

    def _decide_simple(
        self, path: str, is_dir: bool
    ) -> Tuple[Optional[Rule], List[Rule]]:
        """Last matching rule wins (deepest file, then latest line).
        Returns (deciding rule or None, all matching rules in evaluation
        order). A negated deciding rule means 'not ignored'."""
        chain: List[Rule] = []
        for base in self._applicable_bases(path):
            for rule in reversed(self.rules_by_base[base]):
                if rule.matches(path, is_dir):
                    chain.append(rule)
        deciding = chain[0] if chain else None
        return deciding, chain

    def decide(self, path: str, is_dir: bool = False) -> Decision:
        path = normalize_path(path)
        if not path:
            raise ValueError("empty path")
        parts = path.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            rule, _ = self._decide_simple(ancestor, True)
            if rule is not None and not rule.negated:
                _, chain = self._decide_simple(path, is_dir)
                return Decision(
                    path=path,
                    is_dir=is_dir,
                    ignored=True,
                    rule=rule,
                    forced_parent=ancestor,
                    chain=chain,
                )
        rule, chain = self._decide_simple(path, is_dir)
        if rule is None:
            return Decision(path, is_dir, False, None, None, chain)
        return Decision(path, is_dir, not rule.negated, rule, None, chain)


class RepoIgnore(IgnoreSet):
    """Reads nested .gitignore files from a directory tree, like git does."""

    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = os.path.abspath(root)
        self._loaded: Dict[str, bool] = {}

    def _ensure_loaded(self, base: str) -> None:
        if base in self._loaded:
            return
        self._loaded[base] = True
        fs_dir = self.root if base == "" else os.path.join(self.root, *base.split("/"))
        gi = os.path.join(fs_dir, ".gitignore")
        if os.path.isfile(gi):
            with open(gi, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            source = ".gitignore" if base == "" else f"{base}/.gitignore"
            self.add_file_text(text, source=source, base=base)

    def load_all(self) -> None:
        """Load every .gitignore in the tree (audit needs them all, including
        files inside excluded directories, which git would never read)."""
        for dirpath, dirnames, _filenames in os.walk(self.root):
            if ".git" in dirnames:
                dirnames.remove(".git")
            rel = os.path.relpath(dirpath, self.root)
            self._ensure_loaded("" if rel == "." else normalize_path(rel))

    def decide(self, path: str, is_dir: Optional[bool] = None) -> Decision:  # type: ignore[override]
        path = normalize_path(path)
        if not path:
            raise ValueError("empty path")
        parts = path.split("/")
        for i in range(0, len(parts)):
            self._ensure_loaded("/".join(parts[:i]))
        if is_dir is None:
            fs_path = os.path.join(self.root, *parts)
            is_dir = os.path.isdir(fs_path)
        return super().decide(path, is_dir)


def normalize_path(path: str) -> str:
    """CLI/repo paths to git's internal form: '/' separators, no './'."""
    path = path.replace(os.sep, "/")
    if os.sep != "/":
        path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    return path


# --------------------------------------------------------------------------
# Audit: mistakes that silently change what gets ignored.
# --------------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    source: str
    lineno: Optional[int]
    message: str

    def line(self) -> str:
        loc = f"{self.source}:{self.lineno}" if self.lineno else self.source
        return f"{loc}: [{self.kind}] {self.message}"


def _literal_leading_dirs(rule: Rule) -> List[str]:
    """Directory prefixes (relative to rule.base) that every path matching
    this rule must pass through. Empty unless the pattern is anchored and
    starts with glob-free components. Conservative by design."""
    if not rule.anchored:
        return []
    comps = rule.pattern.split("/")
    literal: List[str] = []
    for c in comps:
        if c == "" or any(ch in c for ch in "*?[\\"):
            break
        literal.append(c)
    if len(literal) == len(comps):
        # Fully literal: the last component is the target itself, which a
        # negation may legitimately re-include.
        literal = literal[:-1]
    return ["/".join(literal[:i]) for i in range(1, len(literal) + 1)]


def audit(ignore: IgnoreSet) -> List[Finding]:
    if isinstance(ignore, RepoIgnore):
        ignore.load_all()
    findings: List[Finding] = []
    for base, rules in sorted(ignore.rules_by_base.items()):
        # Duplicate rules: git scans bottom-up, so a later identical rule
        # always wins and the earlier one is unreachable.
        for i, rule in enumerate(rules):
            key = (rule.negated, rule.dir_only, rule.anchored, rule.pattern)
            for later in rules[i + 1 :]:
                if (later.negated, later.dir_only, later.anchored, later.pattern) == key:
                    findings.append(
                        Finding(
                            "duplicate-rule",
                            SEVERITY_WARNING,
                            rule.source,
                            rule.lineno,
                            f"shadowed by identical pattern on line {later.lineno} "
                            "(later rules win in git; this line never takes effect)",
                        )
                    )
                    break
        for rule in rules:
            if _ends_with_unescaped_backslash(rule.pattern):
                findings.append(
                    Finding(
                        "never-matches",
                        SEVERITY_WARNING,
                        rule.source,
                        rule.lineno,
                        "pattern ends with a backslash; git treats a trailing "
                        "backslash as an invalid pattern that never matches",
                    )
                )
            if rule.had_trailing_spaces:
                findings.append(
                    Finding(
                        "trailing-space",
                        SEVERITY_INFO,
                        rule.source,
                        rule.lineno,
                        f"git strips unescaped trailing spaces: this matches "
                        f"'{rule.raw}' (end the line with '\\ ' to match a "
                        "literal trailing space)",
                    )
                )
            if rule.negated:
                for rel_dir in _literal_leading_dirs(rule):
                    ancestor = f"{base}/{rel_dir}" if base else rel_dir
                    dec = ignore.decide(ancestor, is_dir=True)
                    if dec.ignored:
                        findings.append(
                            Finding(
                                "dead-negation",
                                SEVERITY_WARNING,
                                rule.source,
                                rule.lineno,
                                f"parent directory '{ancestor}' is excluded "
                                f"({dec.rule.describe() if dec.rule else '?'}); "
                                "git never descends into excluded directories, "
                                "so this negation can never re-include anything",
                            )
                        )
                        break
    # Repo mode: a .gitignore inside an excluded directory is entirely dead.
    for base in sorted(ignore.rules_by_base):
        if base == "":
            continue
        dec = ignore.decide(base, is_dir=True)
        if dec.ignored:
            src = ignore.rules_by_base[base][0].source if ignore.rules_by_base[base] else base
            findings.append(
                Finding(
                    "dead-file",
                    SEVERITY_WARNING,
                    src,
                    None,
                    f"directory '{base}' is excluded "
                    f"({dec.rule.describe() if dec.rule else '?'}), so none of "
                    "these rules can ever take effect",
                )
            )
    return findings


def _ends_with_unescaped_backslash(pattern: str) -> bool:
    n = 0
    i = len(pattern) - 1
    while i >= 0 and pattern[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_ignore(args: argparse.Namespace) -> IgnoreSet:
    if args.gitignore:
        with open(args.gitignore, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        ig = IgnoreSet()
        ig.add_file_text(text, source=args.gitignore, base="")
        return ig
    root = args.root
    if not os.path.isdir(root):
        raise SystemExit(f"error: not a directory: {root}")
    return RepoIgnore(root)


def _cli_is_dir(ignore: IgnoreSet, path: str, args: argparse.Namespace,
                raw: str = "") -> bool:
    if args.is_dir or raw.endswith("/"):
        return True
    if isinstance(ignore, RepoIgnore):
        return os.path.isdir(os.path.join(ignore.root, *path.split("/")))
    return False


def _decision_to_json(d: Decision) -> dict:
    return {
        "path": d.path,
        "is_dir": d.is_dir,
        "ignored": d.ignored,
        "rule": None
        if d.rule is None
        else {
            "source": d.rule.source,
            "lineno": d.rule.lineno,
            "pattern": d.rule.raw,
            "negated": d.rule.negated,
        },
        "forced_parent": d.forced_parent,
        "chain": [
            {"source": r.source, "lineno": r.lineno, "pattern": r.raw, "negated": r.negated}
            for r in d.chain
        ],
    }


def cmd_check(args: argparse.Namespace) -> int:
    ignore = _build_ignore(args)
    decisions = []
    for raw in args.paths:
        path = normalize_path(raw)
        if not path or path.startswith("../") or path == ".." or os.path.isabs(raw):
            print(f"error: path must be relative to the root: {raw!r}", file=sys.stderr)
            return 2
        decisions.append(ignore.decide(path, _cli_is_dir(ignore, path, args, raw)))
    if args.json:
        print(json.dumps([_decision_to_json(d) for d in decisions], indent=2))
    else:
        for d in decisions:
            if d.ignored:
                print(d.path)
    return 0 if any(d.ignored for d in decisions) else 1


def cmd_explain(args: argparse.Namespace) -> int:
    ignore = _build_ignore(args)
    path = normalize_path(args.path)
    if not path:
        print("error: empty path", file=sys.stderr)
        return 2
    d = ignore.decide(path, _cli_is_dir(ignore, path, args, args.path))
    if args.json:
        print(json.dumps(_decision_to_json(d), indent=2))
    else:
        print("\n".join(d.explain_lines()))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    ignore = _build_ignore(args)
    if isinstance(ignore, RepoIgnore):
        ignore.load_all()
    findings = audit(ignore)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "kind": f.kind,
                        "severity": f.severity,
                        "source": f.source,
                        "lineno": f.lineno,
                        "message": f.message,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        for f in findings:
            print(f.line())
        if not findings:
            print("no findings")
    return 1 if findings else 0


def cmd_scan(args: argparse.Namespace) -> int:
    ignore = _build_ignore(args)
    if not isinstance(ignore, RepoIgnore):
        print("error: scan needs a directory tree (drop --gitignore)", file=sys.stderr)
        return 2
    ignored: List[str] = []
    for dirpath, dirnames, filenames in os.walk(ignore.root):
        rel = os.path.relpath(dirpath, ignore.root)
        rel = "" if rel == "." else normalize_path(rel)
        keep = []
        for d in sorted(dirnames):
            if d == ".git":
                continue
            full = f"{rel}/{d}" if rel else d
            if ignore.decide(full, True).ignored:
                ignored.append(full + "/")
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if f == ".gitignore":
                continue
            full = f"{rel}/{f}" if rel else f
            if ignore.decide(full, False).ignored:
                ignored.append(full)
    if args.json:
        print(json.dumps(ignored, indent=2))
    else:
        for p in ignored:
            print(p)
    return 0 if ignored else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gitignore-check",
        description="Parse, explain, and audit .gitignore rules the way git applies them.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--gitignore", help="single .gitignore file instead of a tree")
    parser.add_argument("--is-dir", action="store_true", help="treat paths as directories")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="print which paths are ignored (like git check-ignore)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("explain", help="show the deciding rule and the full match chain")
    p.add_argument("path")
    p.set_defaults(func=cmd_explain)
    p = sub.add_parser("audit", help="find rules that silently do nothing")
    p.set_defaults(func=cmd_audit)
    p = sub.add_parser("scan", help="walk the tree and print ignored paths (dirs pruned like git)")
    p.set_defaults(func=cmd_scan)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
