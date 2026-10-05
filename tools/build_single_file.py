"""Build the single-file edition of smalldownloader.

    python tools/build_single_file.py [--check] [output]

The package in ``smalldownloader/`` stays the source of truth; this script
merges it into one standalone ``smalldownloader.py`` that can be copied to any
machine and run with a plain ``python smalldownloader.py`` - no install, no
unpacking, no dependencies.

How the merge works (all source-preserving, no AST re-printing):

* module docstrings/comments/formatting are kept verbatim;
* ``from __future__ import annotations`` is emitted once, at the top;
* intra-package imports (``from .core import ...``) are dropped, because the
  modules become one flat namespace in dependency order;
* top-level ``__all__`` lists and ``if __name__ == "__main__"`` blocks are
  dropped (the merged file has its own entry point);
* names that two modules both define are renamed in the later module
  (see :data:`RENAMES`); the build fails if an unexpected clash appears, so
  adding such a name to the package can never silently break the bundle.

``--check`` regenerates in memory and compares against the committed file
(exit code 1 when it is stale). The test-suite calls this, so the checked-in
copy can never drift away from the package.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "smalldownloader")

#: modules to merge, in dependency order
MODULES = ("curlimport", "core", "cli", "gui")

#: per-module renames for names that would otherwise collide
RENAMES = {
    "cli": {},
    "gui": {
        "STATE_COLORS": "GUI_STATE_COLORS",   # cli's palette is colour codes
        "STATE_LABELS": "GUI_STATE_LABELS",   # gui's palette is hex colours
    },
}

BANNER = "# " + "=" * 74


def read_module(name: str) -> str:
    with open(os.path.join(PACKAGE, name + ".py"), "r", encoding="utf-8") as handle:
        return handle.read()


def version_of_package() -> str:
    with open(os.path.join(PACKAGE, "__init__.py"), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("__version__ not found in smalldownloader/__init__.py")


def split_import_names(node: ast.AST) -> set:
    names = set()
    if isinstance(node, ast.Import):
        names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        names.update(alias.asname or alias.name for alias in node.names)
    return names


def bindings(tree: ast.Module) -> tuple:
    """(imported names, defined names) at module level."""
    imported, defined = set(), set()
    for node in tree.body:
        imported |= split_import_names(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    defined.discard("__all__")
    return imported, defined


def is_main_block(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return False
    left, right = test.left, test.comparators[0]
    values = []
    for side in (left, right):
        if isinstance(side, ast.Name):
            values.append(side.id)
        elif isinstance(side, ast.Constant):
            values.append(side.value)
        else:
            values.append(None)
    return "__name__" in values and "__main__" in values


def nested_relative_imports(tree: ast.Module) -> list:
    """Relative imports buried inside functions: the merge cannot keep them."""
    found = []
    top_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and id(node) not in top_level:
            found.append((node.lineno, ast.unparse(node)))
    return found


def drop_lines(tree: ast.Module) -> set:
    """Line numbers (1-based) that must not appear in the merged file."""
    dropped = set()
    for node in tree.body:
        drop = False
        if isinstance(node, ast.ImportFrom):
            if node.level:  # from .core import ...
                drop = True
            if node.module == "__future__":  # emitted once, at the top
                drop = True
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            drop = True  # only relevant for `from module import *`
        elif is_main_block(node):
            drop = True  # the bundle has its own entry point
        if drop:
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                dropped.add(line)
    return dropped


def rename_names(source: str, mapping: dict) -> str:
    """Rename identifiers using tokens, so strings and comments stay untouched."""
    if not mapping:
        return source
    lines = source.splitlines(keepends=True)
    edits = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string in mapping:
            row, col = token.start
            end_row, end_col = token.end
            if row == end_row:
                edits.append((row, col, end_col, mapping[token.string]))
    for row, col, end_col, replacement in sorted(edits, reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + replacement + line[end_col:]
    return "".join(lines)


def strip_lines(source: str, dropped: set) -> str:
    return "".join(
        line for number, line in enumerate(source.splitlines(keepends=True), 1)
        if number not in dropped
    )


def tidy(text: str) -> str:
    """Collapse runs of blank lines left behind by removed statements."""
    out = []
    blanks = 0
    for line in text.splitlines():
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 2:
                out.append("")
    return "\n".join(out).strip("\n")


HEADER = '''#!/usr/bin/env python3
"""
smalldownloader - a tiny downloader with a terminal and a GUI.

THIS FILE IS GENERATED: it is the `smalldownloader/` package merged into one
standalone script by `tools/build_single_file.py`. Do not edit it by hand -
edit the package and rebuild (the test-suite fails if the two drift apart).

Nothing to install: save this file anywhere and run it.

    python smalldownloader.py https://example.com/big.iso -o ~/Downloads
    python smalldownloader.py gui            # open the window
    python smalldownloader.py --help
    python smalldownloader.py --dry-run URL  # size / name / range support

With no arguments it opens the window (double-click also works; rename it to
smalldownloader.pyw on Windows to hide the console). Only the Python standard
library is used, so Python 3.8+ is all you need - no pip, no admin rights.
"""

from __future__ import annotations

__version__ = %r
'''

FOOTER = '''

def _standalone_main(argv=None) -> int:
    """No arguments -> open the window; otherwise behave exactly like the CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        try:
            if run_gui() == 0:
                return 0
        except (KeyboardInterrupt, EOFError):
            return 130
    return main(args)


if __name__ == "__main__":
    sys.exit(_standalone_main())
'''


def build() -> str:
    sections = []
    seen = {}          # name -> module that defined it first
    imported_twice = []  # collisions that are just imports (harmless)

    for name in MODULES:
        source = read_module(name)
        tree = ast.parse(source)

        nested = nested_relative_imports(tree)
        if nested:
            raise RuntimeError(
                "%s has relative imports inside functions (%s) - the merged file "
                "has no package to import from. Use resolve_run_gui()-style "
                "lookups instead."
                % (name, "; ".join("line %d: %s" % item for item in nested))
            )
        imported, defined = bindings(tree)

        clashes = {n for n in defined if n in seen}
        if clashes:
            mapping = RENAMES.get(name, {})
            missing = sorted(clashes - set(mapping))
            if missing:
                raise RuntimeError(
                    "%s defines %s which is already defined in %s - add a rename "
                    "to RENAMES in tools/build_single_file.py"
                    % (name, ", ".join(missing), seen[clashes.pop()])
                )
            source = rename_names(source, mapping)
            defined = {mapping.get(n, n) for n in defined}
        imported_twice.extend(sorted(n for n in imported if n in seen))

        for item in sorted(defined):
            seen.setdefault(item, name)

        source = strip_lines(source, drop_lines(tree))
        sections.append("%s\n# %s.py\n%s\n\n%s" % (BANNER, name, BANNER, tidy(source)))

    body = "\n\n\n".join(sections)
    remaining = [line for line in body.splitlines()
                 if line.strip().startswith(("from .", "import ."))]
    if remaining:
        raise RuntimeError("relative imports survived the merge: %s" % remaining)
    header = HEADER % version_of_package()
    return tidy(header) + "\n\n\n" + body + "\n" + FOOTER.lstrip("\n")


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", default=os.path.join(ROOT, "smalldownloader.py"),
                        help="where to write the bundle (default: ./smalldownloader.py)")
    parser.add_argument("--check", action="store_true",
                        help="verify the existing bundle is up to date")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        bundle = build()
    except RuntimeError as exc:
        print("build failed: %s" % exc, file=sys.stderr)
        return 1

    # a bundle that does not compile is worse than no bundle
    compile(bundle, args.output, "exec")

    if args.check:
        try:
            with open(args.output, "r", encoding="utf-8", newline="\n") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current != bundle:
            print("%s is out of date - run: python tools/build_single_file.py"
                  % os.path.relpath(args.output, ROOT), file=sys.stderr)
            return 1
        if not args.quiet:
            print("%s is up to date (%d lines)" % (
                os.path.relpath(args.output, ROOT), bundle.count("\n")))
        return 0

    write(args.output, bundle)
    if not args.quiet:
        print("wrote %s (%d lines)" % (os.path.relpath(args.output, ROOT),
                                       bundle.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
