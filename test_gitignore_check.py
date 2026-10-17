"""Tests for gitignore_check.

Layers, strongest last:
- unit tests for parsing, matching, precedence, audit, and the CLI;
- every clean vector from git's own wildmatch test suite
  (t/t3070-wildmatch.sh in the git repo), run in both matching modes;
- a differential test that builds a real git repository in a temp dir and
  requires full agreement with `git check-ignore` on both the verdict and
  the identity of the deciding rule for every path (skipped if git is
  absent; present on GitHub's ubuntu and windows runners).
"""

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitignore_check as gic

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "gitignore_check.py")

# (text, pattern, expected_with_WM_PATHNAME, expected_without) from
# t/t3070-wildmatch.sh. git's "wildmatch" mode passes WM_PATHNAME (gitignore
# anchored patterns); its "pathmatch" mode passes no flags (gitignore
# basename patterns). Case-insensitive columns are out of scope.
T3070_ROWS = [
    ('foo', 'foo', True, True),
    ('foo', 'bar', False, False),
    ('', '', True, True),
    ('foo', '???', True, True),
    ('foo', '??', False, False),
    ('foo', '*', True, True),
    ('foo', 'f*', True, True),
    ('foo', '*f', False, False),
    ('foo', '*foo*', True, True),
    ('foobar', '*ob*a*r*', True, True),
    ('aaaaaaabababab', '*ab', True, True),
    ('foo*', 'foo\\*', True, True),
    ('foobar', 'foo\\*bar', False, False),
    ('f\\oo', 'f\\\\oo', True, True),
    ('ball', '*[al]?', True, True),
    ('ten', '[ten]', False, False),
    ('ten', '**[!te]', True, True),
    ('ten', '**[!ten]', False, False),
    ('ten', 't[a-g]n', True, True),
    ('ten', 't[!a-g]n', False, False),
    ('ton', 't[!a-g]n', True, True),
    ('ton', 't[^a-g]n', True, True),
    ('a]b', 'a[]]b', True, True),
    ('a-b', 'a[]-]b', True, True),
    ('a]b', 'a[]-]b', True, True),
    ('aab', 'a[]-]b', False, False),
    ('aab', 'a[]a-]b', True, True),
    (']', ']', True, True),
    ('foo/baz/bar', 'foo*bar', False, True),
    ('foo/baz/bar', 'foo**bar', False, True),
    ('foobazbar', 'foo**bar', True, True),
    ('foo/baz/bar', 'foo/**/bar', True, True),
    ('foo/baz/bar', 'foo/**/**/bar', True, False),
    ('foo/b/a/z/bar', 'foo/**/bar', True, True),
    ('foo/b/a/z/bar', 'foo/**/**/bar', True, True),
    ('foo/bar', 'foo/**/bar', True, False),
    ('foo/bar', 'foo/**/**/bar', True, False),
    ('foo/bar', 'foo?bar', False, True),
    ('foo/bar', 'foo[/]bar', False, True),
    ('foo/bar', 'foo[^a-z]bar', False, True),
    ('foo/bar', 'f[^eiu][^eiu][^eiu][^eiu][^eiu]r', False, True),
    ('foo-bar', 'f[^eiu][^eiu][^eiu][^eiu][^eiu]r', True, True),
    ('foo', '**/foo', True, False),
    ('/foo', '**/foo', True, True),
    ('bar/baz/foo', '**/foo', True, True),
    ('bar/baz/foo', '*/foo', False, True),
    ('foo/bar/baz', '**/bar*', False, True),
    ('deep/foo/bar/baz', '**/bar/*', True, True),
    ('deep/foo/bar/baz/', '**/bar/*', False, True),
    ('deep/foo/bar/baz/', '**/bar/**', True, True),
    ('deep/foo/bar', '**/bar/*', False, False),
    ('deep/foo/bar/', '**/bar/**', True, True),
    ('foo/bar/baz', '**/bar**', False, True),
    ('foo/bar/baz/x', '*/bar/**', True, True),
    ('deep/foo/bar/baz/x', '*/bar/**', False, True),
    ('deep/foo/bar/baz/x', '**/bar/*/*', True, True),
    ('acrt', 'a[c-c]st', False, False),
    ('acrt', 'a[c-c]rt', True, True),
    (']', '[!]-]', False, False),
    ('a', '[!]-]', True, True),
    ('', '\\', False, False),
    ('/\\', '*/\\', False, False),
    ('/\\', '*/\\\\', True, True),
    ('foo', 'foo', True, True),
    ('@foo', '@foo', True, True),
    ('foo', '@foo', False, False),
    ('[ab]', '\\[ab]', True, True),
    ('[ab]', '[[]ab]', True, True),
    ('[ab]', '[[:]ab]', True, True),
    ('[ab]', '[[::]ab]', False, False),
    ('[ab]', '[[:digit]ab]', True, True),
    ('[ab]', '[\\[:]ab]', True, True),
    ('?a?b', '\\??\\?b', True, True),
    ('abc', '\\a\\b\\c', True, True),
    ('foo/bar/baz/to', '**/t[o]', True, True),
    ('a1B', '[[:alpha:]][[:digit:]][[:upper:]]', True, True),
    ('a', '[[:digit:][:upper:][:space:]]', False, False),
    ('A', '[[:digit:][:upper:][:space:]]', True, True),
    ('1', '[[:digit:][:upper:][:space:]]', True, True),
    ('1', '[[:digit:][:upper:][:spaci:]]', False, False),
    (' ', '[[:digit:][:upper:][:space:]]', True, True),
    ('.', '[[:digit:][:upper:][:space:]]', False, False),
    ('.', '[[:digit:][:punct:][:space:]]', True, True),
    ('5', '[[:xdigit:]]', True, True),
    ('f', '[[:xdigit:]]', True, True),
    ('D', '[[:xdigit:]]', True, True),
    ('_', '[[:alnum:][:alpha:][:blank:][:cntrl:][:digit:][:graph:][:lower:][:print:][:punct:][:space:][:upper:][:xdigit:]]', True, True),
    ('.', '[^[:alnum:][:alpha:][:blank:][:cntrl:][:digit:][:lower:][:space:][:upper:][:xdigit:]]', True, True),
    ('5', '[a-c[:digit:]x-z]', True, True),
    ('b', '[a-c[:digit:]x-z]', True, True),
    ('y', '[a-c[:digit:]x-z]', True, True),
    ('q', '[a-c[:digit:]x-z]', False, False),
    (']', '[\\\\-^]', True, True),
    ('[', '[\\\\-^]', False, False),
    ('-', '[\\-_]', True, True),
    (']', '[\\]]', True, True),
    ('\\]', '[\\]]', False, False),
    ('\\', '[\\]]', False, False),
    ('ab', 'a[]b', False, False),
    ('ab', '[!', False, False),
    ('ab', '[-', False, False),
    ('-', '[-]', True, True),
    ('-', '[a-', False, False),
    ('-', '[!a-', False, False),
    ('-', '[--A]', True, True),
    ('5', '[--A]', True, True),
    (' ', '[ --]', True, True),
    ('$', '[ --]', True, True),
    ('-', '[ --]', True, True),
    ('0', '[ --]', False, False),
    ('-', '[---]', True, True),
    ('-', '[------]', True, True),
    ('j', '[a-e-n]', False, False),
    ('-', '[a-e-n]', True, True),
    ('a', '[!------]', True, True),
    ('[', '[]-a]', False, False),
    ('^', '[]-a]', True, True),
    ('^', '[!]-a]', False, False),
    ('[', '[!]-a]', True, True),
    ('^', '[a^bc]', True, True),
    ('-b]', '[a-]b]', True, True),
    ('\\', '[\\]', False, False),
    ('\\', '[\\\\]', True, True),
    ('\\', '[!\\\\]', False, False),
    ('G', '[A-\\\\]', True, True),
    ('aaabbb', 'b*a', False, False),
    ('aabcaa', '*ba*', False, False),
    (',', '[,]', True, True),
    (',', '[\\\\,]', True, True),
    ('\\', '[\\\\,]', True, True),
    ('-', '[,-.]', True, True),
    ('+', '[,-.]', False, False),
    ('-.]', '[,-.]', False, False),
    ('2', '[\\1-\\3]', True, True),
    ('3', '[\\1-\\3]', True, True),
    ('4', '[\\1-\\3]', False, False),
    ('\\', '[[-\\]]', True, True),
    ('[', '[[-\\]]', True, True),
    (']', '[[-\\]]', True, True),
    ('-', '[[-\\]]', False, False),
    ('-adobe-courier-bold-o-normal--12-120-75-75-m-70-iso8859-1', '-*-*-*-*-*-*-12-*-*-*-m-*-*-*', True, True),
    ('-adobe-courier-bold-o-normal--12-120-75-75-X-70-iso8859-1', '-*-*-*-*-*-*-12-*-*-*-m-*-*-*', False, False),
    ('-adobe-courier-bold-o-normal--12-120-75-75-/-70-iso8859-1', '-*-*-*-*-*-*-12-*-*-*-m-*-*-*', False, False),
    ('/adobe/courier/bold/o/normal//12/120/75/75/m/70/iso8859/1', '/*/*/*/*/*/*/12/*/*/*/m/*/*/*', True, True),
    ('/adobe/courier/bold/o/normal//12/120/75/75/X/70/iso8859/1', '/*/*/*/*/*/*/12/*/*/*/m/*/*/*', False, False),
    ('abcd/abcdefg/abcdefghijk/abcdefghijklmnop.txt', '**/*a*b*g*n*t', True, True),
    ('abcd/abcdefg/abcdefghijk/abcdefghijklmnop.txtz', '**/*a*b*g*n*t', False, False),
    ('foo', '*/*/*', False, False),
    ('foo/bar', '*/*/*', False, False),
    ('foo/bba/arr', '*/*/*', True, True),
    ('foo/bb/aa/rr', '*/*/*', False, True),
    ('foo/bb/aa/rr', '**/**/**', True, True),
    ('abcXdefXghi', '*X*i', True, True),
    ('ab/cXd/efXg/hi', '*X*i', False, True),
    ('ab/cXd/efXg/hi', '*/*X*/*/*i', True, True),
    ('ab/cXd/efXg/hi', '**/*X*/**/*i', True, True),
    ('foo', 'fo', False, False),
    ('foo/bar', 'foo/bar', True, True),
    ('foo/bar', 'foo/*', True, True),
    ('foo/bba/arr', 'foo/*', False, True),
    ('foo/bba/arr', 'foo/**', True, True),
    ('foo/bba/arr', 'foo*', False, True),
    ('foo/bba/arr', 'foo/*arr', False, True),
    ('foo/bba/arr', 'foo/**arr', False, True),
    ('foo/bba/arr', 'foo/*z', False, False),
    ('foo/bba/arr', 'foo/**z', False, False),
    ('foo/bar', 'foo?bar', False, True),
    ('foo/bar', 'foo[/]bar', False, True),
    ('foo/bar', 'foo[^a-z]bar', False, True),
    ('ab/cXd/efXg/hi', '*Xg*i', False, True),
    ('a', '[A-Z]', False, False),
    ('A', '[A-Z]', True, True),
    ('A', '[a-z]', False, False),
    ('a', '[a-z]', True, True),
    ('a', '[[:upper:]]', False, False),
    ('A', '[[:upper:]]', True, True),
    ('A', '[[:lower:]]', False, False),
    ('a', '[[:lower:]]', True, True),
    ('A', '[B-Za]', False, False),
    ('a', '[B-Za]', True, True),
    ('A', '[B-a]', False, False),
    ('a', '[B-a]', True, True),
    ('z', '[Z-y]', False, False),
    ('Z', '[Z-y]', True, True)]

def _make_vector_test(text, pattern, exp_path, exp_base):
    def test(self):
        self.assertEqual(
            gic.wildmatch(pattern, text, pathname=True), exp_path,
            f"pathname mode: {pattern!r} vs {text!r}")
        self.assertEqual(
            gic.wildmatch(pattern, text, pathname=False), exp_base,
            f"basename mode: {pattern!r} vs {text!r}")
    return test


class TestT3070Vectors(unittest.TestCase):
    """git's own wildmatch vectors: 184 cases x 2 modes."""


for _i, (_text, _pattern, _ep, _eb) in enumerate(T3070_ROWS):
    setattr(TestT3070Vectors, "test_%03d" % _i,
            _make_vector_test(_text, _pattern, _ep, _eb))


def decide(lines, path, is_dir=False, source=".gitignore"):
    ig = gic.IgnoreSet()
    ig.add_file_text("\n".join(lines), source=source, base="")
    return ig.decide(path, is_dir)


class TestParse(unittest.TestCase):
    def rules(self, text):
        return gic.parse_lines(text)

    def test_blank_and_comment_lines_skipped(self):
        self.assertEqual(self.rules("\n\n# comment\n   \n"), [])

    def test_escaped_hash_and_bang(self):
        (r1,) = self.rules("\\#todo")
        self.assertFalse(r1.negated)
        self.assertTrue(gic.wildmatch(r1.pattern, "#todo"))
        (r2,) = self.rules("\\!important!.txt")
        self.assertFalse(r2.negated)
        self.assertTrue(gic.wildmatch(r2.pattern, "!important!.txt"))

    def test_trailing_spaces_stripped_unless_escaped(self):
        # plain trailing spaces: stripped (git trim_trailing_spaces)
        (r,) = self.rules("foo   ")
        self.assertEqual(r.pattern, "foo")
        self.assertTrue(r.had_trailing_spaces)
        # 'foo\ ': the backslash quotes the space, so it is kept and the
        # pattern matches 'foo ' with a literal trailing space
        (r,) = self.rules("foo\\ ")
        self.assertEqual(r.pattern, "foo\\ ")
        self.assertFalse(r.had_trailing_spaces)
        # 'foo\\ ': the escaped backslash means the space is NOT quoted
        (r,) = self.rules("foo\\\\ ")
        self.assertEqual(r.pattern, "foo\\\\")
        self.assertTrue(r.had_trailing_spaces)
        # a trailing backslash aborts the trim entirely (dir.c behavior)
        (r,) = self.rules("foo \\")
        self.assertEqual(r.pattern, "foo \\")
        # tabs are never stripped (only spaces are)
        (r,) = self.rules("foo\t")
        self.assertEqual(r.pattern, "foo\t")

    def test_crlf_and_bom(self):
        rules = self.rules("\ufeffa.txt\r\nb.txt\r\n")
        self.assertEqual([r.pattern for r in rules], ["a.txt", "b.txt"])

    def test_dir_only_and_anchored_flags(self):
        (r,) = self.rules("foo/")
        self.assertTrue((r.dir_only, r.anchored) == (True, False))
        (r,) = self.rules("/foo")
        self.assertEqual(r.pattern, "foo")
        self.assertTrue(r.anchored)
        (r,) = self.rules("doc/frotz")
        self.assertTrue(r.anchored)
        (r,) = self.rules("!/keep.log")
        self.assertTrue(r.negated and r.anchored)

    def test_pattern_that_is_just_slash_or_bang_matches_nothing(self):
        (r,) = self.rules("/")
        self.assertFalse(r.matches("foo", True) or r.matches("foo", False))
        (r,) = self.rules("!")
        self.assertFalse(r.matches("foo", False))

    def test_lineno_and_source_recorded(self):
        rules = self.rules("# c\n*.log\n\n!keep.log\n")
        self.assertEqual([(r.lineno, r.source) for r in rules],
                         [(2, "<string>"), (4, "<string>")])


class TestDocExamples(unittest.TestCase):
    """Every worked example in gitignore(5): PATTERN FORMAT and EXAMPLES."""

    def test_hello_star_vs_slash_hello_star(self):
        self.assertTrue(decide(["hello.*"], "hello.txt").ignored)
        self.assertTrue(decide(["hello.*"], "a/hello.java").ignored)
        self.assertTrue(decide(["/hello.*"], "hello.txt").ignored)
        self.assertTrue(decide(["/hello.*"], "hello.c").ignored)
        self.assertFalse(decide(["/hello.*"], "a/hello.java").ignored)

    def test_foo_slash_matches_only_directories(self):
        self.assertTrue(decide(["foo/"], "foo", True).ignored)
        self.assertTrue(decide(["foo/"], "a/foo", True).ignored)
        self.assertFalse(decide(["foo/"], "foo", False).ignored)
        # and everything underneath it, via the parent-directory rule
        d = decide(["foo/"], "foo/bar/baz.c")
        self.assertTrue(d.ignored)
        self.assertEqual(d.forced_parent, "foo")

    def test_doc_frotz_anchoring(self):
        self.assertTrue(decide(["doc/frotz"], "doc/frotz").ignored)
        self.assertFalse(decide(["doc/frotz"], "a/doc/frotz").ignored)
        self.assertTrue(decide(["frotz/"], "a/frotz", True).ignored)

    def test_foo_star_example(self):
        # gitignore(5): foo/* matches foo/test.json and the directory
        # foo/bar, but the pattern does not match foo/bar/hello.c itself.
        (rule,) = gic.parse_lines("foo/*")
        self.assertFalse(rule.matches("foo/bar/hello.c", False))
        # ...but since foo/* excludes the directory foo/bar, the file under
        # it is ignored anyway (verified against git check-ignore).
        self.assertTrue(decide(["foo/*"], "foo/test.json").ignored)
        self.assertTrue(decide(["foo/*"], "foo/bar", True).ignored)
        d = decide(["foo/*"], "foo/bar/hello.c")
        self.assertTrue(d.ignored)
        self.assertEqual(d.forced_parent, "foo/bar")

    def test_exclude_and_documentation_example(self):
        # The gitignore(5) EXAMPLES transcript: *.[oa] at the root plus a
        # nested Documentation/.gitignore with *.html and !foo.html.
        ig = gic.IgnoreSet()
        ig.add_file_text("*.[oa]", ".gitignore", "")
        ig.add_file_text("*.html\n!foo.html", "Documentation/.gitignore", "Documentation")
        self.assertTrue(ig.decide("file.o").ignored)
        self.assertTrue(ig.decide("lib.a").ignored)
        self.assertTrue(ig.decide("src/internal.o").ignored)
        self.assertTrue(ig.decide("Documentation/gitignore.html").ignored)
        d = ig.decide("Documentation/foo.html")
        self.assertFalse(d.ignored)
        self.assertEqual((d.rule.lineno, d.rule.raw), (2, "!foo.html"))

    def test_vmlinux_nested_negation_example(self):
        ig = gic.IgnoreSet()
        ig.add_file_text("vmlinux*", ".gitignore", "")
        ig.add_file_text("!/vmlinux*", "arch/foo/kernel/.gitignore", "arch/foo/kernel")
        self.assertTrue(ig.decide("vmlinux.lds.S").ignored)
        self.assertTrue(ig.decide("arch/vmlinux.lds.S").ignored)
        self.assertFalse(ig.decide("arch/foo/kernel/vmlinux.lds.S").ignored)

    def test_exclude_everything_except_foo_bar_example(self):
        # gitignore(5) example; verdicts verified against git check-ignore.
        lines = ["/*", "!/foo", "/foo/*", "!/foo/bar"]
        self.assertTrue(decide(lines, "a.txt").ignored)
        self.assertFalse(decide(lines, "foo", True).ignored)
        self.assertTrue(decide(lines, "foo/a.txt").ignored)
        self.assertFalse(decide(lines, "foo/bar", True).ignored)
        self.assertFalse(decide(lines, "foo/bar/a.txt").ignored)

    def test_doublestar_examples_from_pattern_format(self):
        self.assertTrue(decide(["**/foo"], "foo").ignored)
        self.assertTrue(decide(["**/foo"], "anywhere/foo").ignored)
        self.assertTrue(decide(["**/foo/bar"], "x/foo/bar").ignored)
        self.assertFalse(decide(["**/foo/bar"], "x/y/foo/z/bar").ignored)
        self.assertTrue(decide(["abc/**"], "abc/x/y/z").ignored)
        for path in ("a/b", "a/x/b", "a/x/y/b"):
            self.assertTrue(decide(["a/**/b"], path).ignored, path)
        self.assertFalse(decide(["a/**/b"], "x/a/b").ignored)


class TestPrecedence(unittest.TestCase):
    def test_later_line_wins_within_a_file(self):
        ig = gic.IgnoreSet()
        ig.add_file_text("*.log\n!keep.log", ".gitignore", "")
        d = ig.decide("keep.log")
        self.assertFalse(d.ignored)
        self.assertEqual(d.rule.raw, "!keep.log")
        self.assertTrue(ig.decide("other.log").ignored)

    def test_last_of_three_wins(self):
        self.assertTrue(decide(["a.txt", "!a.txt", "a.txt"], "a.txt").ignored)
        self.assertFalse(decide(["!a.txt", "a.txt", "!a.txt"], "a.txt").ignored)

    def test_deeper_file_beats_shallower(self):
        ig = gic.IgnoreSet()
        ig.add_file_text("*.txt", ".gitignore", "")
        ig.add_file_text("!a.txt", "sub/.gitignore", "sub")
        self.assertFalse(ig.decide("sub/a.txt").ignored)
        self.assertTrue(ig.decide("a.txt").ignored)
        self.assertTrue(ig.decide("sub/b.txt").ignored)

    def test_negation_cannot_reinclude_under_excluded_dir(self):
        # gitignore(5): "It is not possible to re-include a file if a parent
        # directory of that file is excluded."
        ig = gic.IgnoreSet()
        ig.add_file_text("build/\n!build/keep.txt", ".gitignore", "")
        d = ig.decide("build/keep.txt")
        self.assertTrue(d.ignored)
        self.assertEqual(d.forced_parent, "build")
        self.assertEqual(d.rule.raw, "build/")
        self.assertEqual(d.rule.lineno, 1)

    def test_reincluded_parent_makes_negation_live(self):
        ig = gic.IgnoreSet()
        ig.add_file_text("build/\n!/build/\n!build/keep.txt", ".gitignore", "")
        self.assertFalse(ig.decide("build/keep.txt").ignored)
        self.assertFalse(ig.decide("build/other.txt").ignored)

    def test_rules_scoped_to_their_directory(self):
        ig = gic.IgnoreSet()
        ig.add_file_text("*.o", "sub/.gitignore", "sub")
        self.assertFalse(ig.decide("a.o").ignored)
        self.assertTrue(ig.decide("sub/a.o").ignored)
        self.assertTrue(ig.decide("sub/deep/a.o").ignored)
        self.assertFalse(ig.decide("sub", True).ignored)


class TestFnmatchDivergence(unittest.TestCase):
    """Pin where Python's stdlib fnmatch gives the wrong gitignore answer."""

    def test_stdlib_star_crosses_slashes(self):
        # fnmatch's '*' matches '/'; git's never does in pathname mode.
        self.assertTrue(fnmatch.fnmatchcase("foo/bba/arr", "foo/*arr"))
        self.assertFalse(gic.wildmatch("foo/*arr", "foo/bba/arr", pathname=True))
        self.assertTrue(fnmatch.fnmatchcase("foo/bar", "foo*bar"))
        self.assertFalse(gic.wildmatch("foo*bar", "foo/bar", pathname=True))

    def test_stdlib_has_no_doublestar(self):
        # In fnmatch '**' is just two '*'s and crosses slashes anywhere; git
        # only honors '**' in its documented positions, so 'a**b' behaves
        # like 'a*b': it cannot cross '/'.
        self.assertTrue(fnmatch.fnmatchcase("a/x/b", "a**b"))
        self.assertFalse(gic.wildmatch("a**b", "a/x/b", pathname=True))
        self.assertTrue(gic.wildmatch("a**b", "axxb", pathname=True))

    def test_stdlib_has_no_negation_anchoring_or_dir_only(self):
        # These are gitignore-level concepts; fnmatch sees literal text.
        self.assertFalse(fnmatch.fnmatchcase("keep.log", "!keep.log"))
        d = decide(["!keep.log", "*.log"], "keep.log")  # note order: *.log last
        self.assertTrue(d.ignored)


class TestAudit(unittest.TestCase):
    def kinds(self, text, base=""):
        ig = gic.IgnoreSet()
        ig.add_file_text(text, ".gitignore" if not base else f"{base}/.gitignore", base)
        return gic.audit(ig)

    def test_duplicate_rule_shadowed_by_later_identical(self):
        findings = self.kinds("*.log\n*.log\n")
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual((f.kind, f.lineno), ("duplicate-rule", 1))
        self.assertIn("line 2", f.message)

    def test_trailing_backslash_never_matches(self):
        findings = self.kinds("foo\\\n")
        self.assertEqual([f.kind for f in findings], ["never-matches"])

    def test_trailing_space_info(self):
        findings = self.kinds("foo \n")
        self.assertEqual([(f.kind, f.severity) for f in findings],
                         [("trailing-space", "info")])

    def test_dead_negation_under_excluded_dir(self):
        findings = self.kinds("build/\n!build/keep.txt\n")
        self.assertEqual([f.kind for f in findings], ["dead-negation"])
        self.assertIn("build", findings[0].message)

    def test_live_negation_after_reincluded_parent_is_not_flagged(self):
        self.assertEqual(self.kinds("build/\n!/build/\n!build/keep.txt\n"), [])

    def test_nonanchored_negation_not_flagged(self):
        # '!keep.log' can fire anywhere; nothing static to prove it dead.
        self.assertEqual(self.kinds("build/\n!keep.log\n"), [])

    def test_clean_file_has_no_findings(self):
        self.assertEqual(self.kinds("*.log\n!keep.log\nbuild/\n"), [])

    def test_dead_file_in_excluded_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as fh:
                fh.write("build/\n")
            os.mkdir(os.path.join(tmp, "build"))
            with open(os.path.join(tmp, "build", ".gitignore"), "w") as fh:
                fh.write("*.o\n!keep.o\n")
            findings = gic.audit(gic.RepoIgnore(tmp))
            self.assertEqual([f.kind for f in findings], ["dead-file"])
            self.assertIn("build", findings[0].message)


class TestCLI(unittest.TestCase):
    def run_cli(self, *argv, cwd=None):
        return subprocess.run(
            [sys.executable, SCRIPT, *argv],
            capture_output=True, text=True, cwd=cwd or HERE)

    def make_tree(self, tmp):
        with open(os.path.join(tmp, ".gitignore"), "w") as fh:
            fh.write("*.log\n!keep.log\nbuild/\n!build/keep.log\n")
        os.mkdir(os.path.join(tmp, "build"))
        for p in ("a.log", "keep.log", "ok.txt", "build/x.txt", "build/keep.log"):
            with open(os.path.join(tmp, p), "w") as fh:
                fh.write("x")

    def test_check_exit_codes_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "check", "a.log", "ok.txt")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "a.log")
            r = self.run_cli("--root", tmp, "check", "ok.txt", "keep.log")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout, "")

    def test_check_parent_dir_rule_matches_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "check", "build/keep.log")
            self.assertEqual(r.returncode, 0)  # still ignored, like git

    def test_check_trailing_slash_marks_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as fh:
                fh.write("foo/\n")
            self.assertEqual(self.run_cli("--root", tmp, "check", "foo/").returncode, 0)
            self.assertEqual(self.run_cli("--root", tmp, "check", "foo").returncode, 1)

    def test_explain_shows_deciding_rule_and_dead_negation_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "explain", "build/keep.log")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verdict: IGNORED", r.stdout)
            self.assertIn("deciding rule: .gitignore:3: build/", r.stdout)
            self.assertIn('parent directory "build" is excluded', r.stdout)
            self.assertIn("!build/keep.log", r.stdout)

    def test_explain_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "explain", "ok.txt")
            self.assertIn("verdict: NOT IGNORED", r.stdout)

    def test_audit_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as fh:
                fh.write("*.log\n*.log\n")
            r = self.run_cli("--root", tmp, "audit")
            self.assertEqual(r.returncode, 1)
            self.assertIn("duplicate-rule", r.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".gitignore"), "w") as fh:
                fh.write("*.log\n")
            self.assertEqual(self.run_cli("--root", tmp, "audit").returncode, 0)

    def test_json_output_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "--json", "check", "a.log", "ok.txt")
            data = json.loads(r.stdout)
            self.assertEqual([d["ignored"] for d in data], [True, False])
            r = self.run_cli("--root", tmp, "--json", "audit")
            self.assertIsInstance(json.loads(r.stdout), list)

    def test_scan_walks_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "scan")
            self.assertEqual(r.returncode, 0)
            got = set(r.stdout.split())
            self.assertIn("a.log", got)
            self.assertIn("build/", got)  # pruned: no build/x.txt listing
            self.assertNotIn("build/x.txt", got)
            self.assertNotIn("keep.log", got)

    def test_single_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            gi = os.path.join(tmp, "ignore.txt")
            with open(gi, "w") as fh:
                fh.write("*.log\n")
            r = self.run_cli("--gitignore", gi, "check", "x/a.log")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "x/a.log")

    def test_bad_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_tree(tmp)
            r = self.run_cli("--root", tmp, "check", "../escape")
            self.assertEqual(r.returncode, 2)


@unittest.skipUnless(shutil.which("git"), "git binary not available")
class TestAgainstRealGit(unittest.TestCase):
    """Differential test: full agreement with `git check-ignore` on verdict
    AND deciding-rule identity, in a real repository."""

    ROOT_GITIGNORE = "\n".join([
        "# logs are noise",
        "*.log",
        "!keep.log",
        "build/",
        "/root-only.txt",
        "doc/**/*.pdf",
        "**/gen_*.py",
        "src/*.o",
        "vmlinux*",
        "!build/keep.log",
        "trailing.txt   ",
        "",
    ])
    DOC_GITIGNORE = "*.html\n!index.html\n"
    DEEP_GITIGNORE = "!/vmlinux*\n"

    FILES = [
        "a.log", "keep.log", "root-only.txt", "trailing.txt", "gen_1.py",
        "README.md", "vmlinux.lds.S",
        "build/x.txt", "build/keep.log",
        "doc/a.pdf", "doc/index.html", "doc/page.html", "doc/notes.txt",
        "doc/x/y/a.pdf", "doc/sub/page.html", "doc/sub/index.html",
        "lib/gen_2.py", "lib/x/gen_3.py",
        "src/a.o", "src/keep.log", "src/x/b.o",
        "sub/root-only.txt", "sub/vmlinuxX",
        "sub/deep/vmlinux.lds.S",
    ]
    QUERIES = FILES + ["build", "doc", "sub", "sub/deep"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        self.home = os.path.join(tmp, "home")
        os.mkdir(self.home)
        self.repo = os.path.join(tmp, "repo")
        os.mkdir(self.repo)
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home,
                        GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=self.env, check=True,
                       capture_output=True)
        self.empty_excludes = os.path.join(tmp, "empty-excludes")
        open(self.empty_excludes, "w").close()

        def put(rel, text=""):
            full = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)

        put(".gitignore", self.ROOT_GITIGNORE)
        put("doc/.gitignore", self.DOC_GITIGNORE)
        put("sub/deep/.gitignore", self.DEEP_GITIGNORE)
        for rel in self.FILES:
            put(rel, "x")

    def git_check_ignore_verbose(self):
        # -z -v: NUL-separated records <source>\0<lineno>\0<pattern>\0<path>\0
        proc = subprocess.run(
            ["git", "-c",
             "core.excludesFile=" + self.empty_excludes.replace(os.sep, "/"),
             "check-ignore", "-z", "-v", "--stdin"],
            input="\0".join(self.QUERIES).encode() + b"\0",
            capture_output=True, cwd=self.repo, env=self.env, check=True)
        out = {}
        fields = proc.stdout.decode().split("\0")
        for i in range(0, len(fields) - 4, 4):
            source, lineno, pattern, path = fields[i:i + 4]
            out[path] = (source, lineno, pattern)
        return out

    def test_verdict_and_deciding_rule_agreement(self):
        git = self.git_check_ignore_verbose()
        ours = gic.RepoIgnore(self.repo)
        mismatches = []
        for path in self.QUERIES:
            d = ours.decide(path)  # is_dir from disk, like git's resolve_dtype
            hit = git.get(path)
            git_ignored = hit is not None and not hit[2].startswith("!")
            if d.ignored != git_ignored:
                mismatches.append(f"{path}: ours ignored={d.ignored}, git={hit}")
                continue
            if hit is not None:
                # git reports the last matching pattern; it must be ours.
                if d.rule is None:
                    mismatches.append(f"{path}: git matched {hit}, we matched nothing")
                    continue
                got = (d.rule.source, str(d.rule.lineno), d.rule.raw)
                if got != hit:
                    mismatches.append(f"{path}: ours rule={got}, git rule={hit}")
            elif d.rule is not None:
                mismatches.append(f"{path}: we matched {d.rule.describe()}, git matched nothing")
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
