# gitignore-check

Parse, explain, and audit `.gitignore` rules the way **git actually applies them** — not the way they look like they should work.

`.gitignore` is deceptively subtle. Rules that look right silently do nothing:

```gitignore
build/
!build/keep.txt    # looks like it keeps build/keep.txt. It does nothing.
```

git never descends into an excluded directory, so a negation can **never** re-include a file under an excluded parent — no matter where it is written (gitignore(5)). `gitignore-check` shows you what git will really do, and flags the rules that are dead on arrival.

```console
$ gitignore-check explain build/keep.txt
path: build/keep.txt (file)
verdict: IGNORED
deciding rule: .gitignore:1: build/
note: parent directory "build" is excluded, so no rule matching this path can re-include it (gitignore(5))
rules that match the path itself (none take effect):
  .gitignore:2: !build/keep.txt  [negation -> include]

$ gitignore-check audit
.gitignore:2: [dead-negation] parent directory 'build' is excluded (.gitignore:1: build/); git never descends into excluded directories, so this negation can never re-include anything
```

## What it does

- **`check PATH...`** — print which paths are ignored, with `git check-ignore`'s exit codes (0 = at least one ignored, 1 = none).
- **`explain PATH`** — the verdict, the exact `file:line` that decided it, every rule that matched, and the parent-directory note when a negation is being overridden.
- **`audit`** — static findings for rules that silently do nothing:
  - `dead-negation` — a negation whose target always sits under an excluded directory;
  - `duplicate-rule` — an identical pattern appears later in the same file, so this line never takes effect (in git the **last** matching line wins);
  - `dead-file` — a whole `.gitignore` inside an excluded directory;
  - `never-matches` — a pattern ending in a backslash, which git treats as invalid;
  - `trailing-space` — unescaped trailing spaces that git strips (you probably meant the version with the space).
- **`scan`** — walk a tree and list ignored paths, pruning ignored directories exactly like git.

Python 3.9+, standard library only, no network, no git binary required. `--json` on every command. Works on a whole tree (`--root`, reads nested `.gitignore` files with git's precedence rules) or on one file (`--gitignore FILE`).

## Why not just `fnmatch`?

Python's stdlib [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) gives wrong answers for gitignore semantics, and the tests pin the divergence:

| pattern / path | stdlib `fnmatch` | git |
|---|---|---|
| `foo/*arr` vs `foo/bba/arr` | matches (`*` crosses `/`) | no match |
| `a**b` vs `a/x/b` | matches (`**` is just two `*`s) | no match (`**` is special only in its documented positions) |
| `!keep.log` | literal text | negation |
| `build/` | literal text | directories only |

git does not use `fnmatch(3)` either; it uses its own `wildmatch`. This tool is a Python port of that algorithm, not a regex approximation.

## Faithfulness, and how it is tested

The implementation mirrors git's own source, and the test suite proves it in three layers (227 tests total):

1. **Spec vectors.** Every clean vector from git's own wildmatch test suite, [`t/t3070-wildmatch.sh`](https://github.com/git/git/blob/master/t/t3070-wildmatch.sh) — 184 cases, each run in both matching modes git uses (pathname mode for anchored patterns, basename mode for the rest) — plus every worked example in the [`gitignore(5)` manual](https://git-scm.com/docs/gitignore).
2. **Source-level port.** Matching is a line-level port of [`wildmatch.c` (`dowild`)](https://github.com/git/git/blob/master/wildmatch.c); parsing mirrors [`dir.c`](https://github.com/git/git/blob/master/dir.c) (`trim_trailing_spaces`, `parse_path_pattern`, `last_matching_pattern_from_list`): BOM and CRLF handling, unescaped-trailing-space stripping, `\!` / `\#` escaping, `!` negation, trailing-`/` directory-only patterns, anchoring by any leading or middle `/`, deepest-file-then-last-line precedence.
3. **Differential test against real git.** The suite builds a throwaway repository with nested `.gitignore` files and a corpus of tricky paths, then requires **full agreement with `git check-ignore -v` on both the verdict and the identity (`file:line:pattern`) of the deciding rule for every path** — including the dead-negation case above, where git's behavior surprises most people. CI runs it on `ubuntu-latest` and `windows-latest` x Python 3.9 and 3.13 (git is preinstalled on those runners; the test skips itself where git is absent).

## Scope and limitations

- Reads `.gitignore` files only. `$GIT_DIR/info/exclude` and `core.excludesFile` are out of scope (their pattern semantics are identical; only their precedence differs — lowest first, per gitignore(5)).
- Case-sensitive matching, like git with the default `core.ignoreCase=false`. `WM_CASEFOLD` is not implemented.
- `check` decides whether a path *would* be ignored; whether git actually skips a tracked file is a separate matter (`--no-index` territory).
- The `dead-negation` check is deliberately conservative: it only flags negations whose targets provably sit under an excluded directory, so it never cries wolf on patterns that might match elsewhere.

## License

MIT — see [LICENSE](LICENSE).
