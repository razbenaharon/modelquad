# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""No test in this repository asks `platform` a question that spawns.

The subject is a console window that flashes on the user's desktop.
The worst place for the regression suite to open one is *itself*, and it did:
a review found `tests/test_scripts_no_window.py` calling
`platform.platform()` in-process, on the **default** test path, in the file
whose entire subject is that nothing should open a console.

**Why an ordinary-looking call spawns.** On Windows every `platform` function
that goes through `uname()` reaches `platform.win32_ver()`, which calls
`_syscmd_ver()` unconditionally. `_syscmd_ver()` runs::

    subprocess.check_output('ver', ..., shell=True)   # no creationflags

with no console suppression, so from a console-less parent -- an agent
harness, a service, a detached job runner -- that `cmd.exe` allocates a console
and the user sees a window. It is CPython's spawn, not ModelQuad's, so the
`creationflags` invariants in `test_procutil.py` and `test_scripts_no_window.py`
cannot see it: both recognise a literal `subprocess.<method>(...)` call, and
this one is behind a stdlib function that looks like a lookup.

**The forbidden set was measured, not recalled.** One cold interpreter per
function on Windows 11 / CPython 3.11.9, with `subprocess.check_output`
instrumented, 2026-09-10::

    platform()   -> 2 spawns      release()      -> 1 spawn
    system()     -> 1 spawn       node()         -> 1 spawn
    version()    -> 1 spawn       processor()    -> 1 spawn
    machine()    -> 1 spawn       architecture() -> 0
    uname()      -> 1 spawn       python_version() -> 0
    win32_ver()  -> 1 spawn

So the list below is nine functions, not the five or six that get quoted from
memory -- `release()`, `node()` and `processor()` reach it too. `architecture()`
and `python_version()` do not go through `uname()` at all, so the second rule
below -- the reference rule -- leaves them alone.

**Which is not the same as being callable, and until round 10 this docstring
said it was.** It called them "deliberately allowed", unqualified,
while the primary rule rejects *every* `import platform` and *every* `from
platform import ...` in non-exempt test support, whatever the module is used
for afterwards. There is therefore no ordinary spelling that reaches
`architecture()` at all, and the documentation and the enforcement were
describing two different guards. What is true is narrower: the two functions
are reachable exactly where the reference rule is the only rule left, which is
inside a `PLATFORM_IMPORT_EXEMPT` module. Everywhere else the approved way to
learn what `platform` reports is `conftest.platform_reference()`, for these two
functions the same as for the nine.

**Two rules, not one, because call-site matching alone cannot hold.**
The first version of this scan matched call sites: `platform.system()`, and a
bare `system()` from `from platform import system`. A review pointed out
that the forms which evade that are not exotic, they are ordinary Python --
`f = platform.system` and then `f()` somewhere else, `getattr(platform,
"system")()`, `from platform import *`. Each is one line of unsuspicious code,
and the evasion tail has no end: a dict of callables, a `partial`, a class
attribute, a decorator. A scan that promises *no ModelQuad-owned test code
triggers this path* cannot rest on enumerating call syntax.

So the primary rule moves one step upstream, to **acquisition**: test-support
code may not get its hands on the `platform` module at all. Every use, however
it is spelled, needs the module first, and there are only so many ways to
obtain it -- `import`, `from ... import`, `importlib.import_module`,
`__import__`, `sys.modules`. All five are rejected. The approved way to learn
what `platform` reports is `conftest.platform_reference()`, which reads it in
a hidden child.

**Each of those five is rejected under a rename, too.** The first
version of the acquisition rule matched the importer by its callee name and
the module map by subscript, and review found three ordinary spellings it
returned nothing at all for::

    from importlib import import_module as load;  m = load("platform")
    load = importlib.import_module;               m = load("platform")
    m = sys.modules.get("platform")

The choice was to narrow the documented guarantee to what the scan caught, or
to widen the scan. Widening, because narrowing would concede exactly the class
the acquisition rule was introduced to close: an `as` clause on an import and
a `dict.get` are not evasion, they are how people write Python, and a guard
that stops `import platform` while waving through `load("platform")` is a
spelling rule wearing a guarantee's clothes. So the importer is now tracked by
*value* through an assignment chain, the same fixed-point walk the module
aliases get; `get`/`pop`/`setdefault` join subscripting as module-map
accessors; and an aliased map (`mm = sys.modules`, then `mm["platform"]` --
missed even by the older subscript rule) is followed as well. `"platform" in
sys.modules` and `sys.modules.keys()` are deliberately *not* offences: they ask
about the map without yielding the module, so they acquire nothing.

**Tracked by provenance rather than by name, and then the claim narrowed to
match.** Rounds 5, 6 and 7 each found more acquisition forms, and each
time the answer was a bigger scan. That is an infinite regress: a static scan
over Python cannot be complete, because `getattr(__import__("sys"),
"modules")["platform"]` is legal and so is a helper in a module this scan never
opens. Round 7 also found the regress running the other way -- matching *any*
attribute called `modules` or `import_module` flagged `self.modules` and a
fixture's `import_module`, code with no `platform` in it at all -- and a guard
that cries wolf gets deleted by the next person who trips over it.

So the mechanism was fixed once more, and then the *claim* was cut down to what
the mechanism enforces. The mechanism: `Provenance` reads a file's import
statements to learn which names hold `sys`, `importlib`, `builtins`, the module
map and an importer, and follows renames from there. `.modules` counts on a
name bound to `sys` and nowhere else; `.import_module` on a name bound to
`importlib` and nowhere else. That one change caught `from sys import modules
as mm` (a renamed *import* of the map, which the assignment-seeded aliases
never saw) and `import_module(name="platform")` (both importers spell the
parameter `name`), and stopped the three false positives, together.

The call-site rule is kept as the second layer, because the acquisition rule
can need an exemption (`PLATFORM_IMPORT_EXEMPT`, empty today) and an exemption with no
rule left inside it is a hole with a comment on it. Inside an exempt module the
offence is *referencing* a spawning attribute, not calling it, so
`f = platform.system` fails on its own line whether or not the scan can see
where `f` is finally called.

**What "a rename" means, said once.** Three of the rules below follow a
value across a rename, and until round 9 *rename* silently meant `a = b` and
nothing else -- so `load: object = importlib.import_module` was invisible while
the identical line without the annotation was flagged. An annotation is not
evasion, it is what a typed codebase writes, and the docstring already promised
that statically visible renames are inside the boundary. Rather than list
forms, the boundary is now stated: a rename is **a name bound to an expression
written beside it** -- `a = b`, `a: T = b`, `(a := b)`, and `a, b = x, y`
against a display of the same shape, one starred target included, since the
positions before and after it are still fixed by counting from each end. A
binding whose value some runtime step *produces* is not a rename and is in the
residue below.

**The guarantee, exactly, and no wider.** For every file in the scanned set,
reading that file's own syntax tree and nothing else, this scan fails on:

* `import platform`, `import platform as p`, and `from platform import ...`
  including `*`;
* a call to `__import__`, or to a name this file imports from `importlib` as
  `import_module` -- under any rename, in the exact sense above, and through
  chains of them -- with `"platform"` written as a string literal, positionally
  or as `name=`;
* a subscript, or a `get`/`pop`/`setdefault` of `"platform"`, on `sys.modules`
  reached through a name this file binds to `sys` or to the map itself;
* any reference to one of the nine `UNAME_REACHING` attributes on a name the
  rules above bound to the module or that a chain of renames carried it to,
  `getattr` of one with a literal name, or a bare name a `from platform import`
  brought in -- whether or not it is called.

**It does not claim that a run of this suite spawns no `cmd /c ver`, and it
does not claim that a test cannot reach `platform`.** It claims that no
ModelQuad-owned test file *spells* its way to the module in syntax that file
itself makes visible. The per-file qualifier is load-bearing rather than
decorative: a name is only the importer, the module map or the module
if *this* file's imports say so, so the scanned set is checked one file at a
time and never as a whole. Two scanned files that together reach `platform`,
neither of which reaches it alone, are outside the claim. That is narrower than
round 5's ambition and it is the width that is actually true; the list below is
what falls outside it, by construction rather than by oversight.

* Product code under `src/`. Scanning `src/` statically would not help here:
  a deliberate `platform` cache read and a live one look the same to a
  source scan.
* **Third-party code the suite imports.** Nothing here reads site-packages, so
  a dependency that calls `platform.system()` at import time is invisible to
  it. The `pyreadline3` note below is exactly that case, and it is why the
  claim is scoped to ModelQuad-owned test code rather than to a run.
* **Acquisition through a value the scan cannot name.** The importer, the
  module map and the module are followed across renames, and no further. A
  `functools.partial(importlib.import_module, "platform")`, an importer parked
  in a dict or on a class, or a module name computed rather than written
  (`load(("plat" + "form"))`) all pass. This is the honest residue rather than
  a claim of completeness: arbitrary runtime indirection is outside the
  boundary, statically visible renames are inside it, and the line between them
  is where the scan stops following a value.
* **A binding whose value is produced rather than written.** The other
  half of the rename definition above, and the same line drawn from the other
  side. `for load in (importlib.import_module,)` binds an element pulled out of
  an iterable; `with ctx as load` binds `__enter__`'s return, which is by
  definition not the expression written; a comprehension is the `for` case with
  different punctuation; `match subject: case load:` binds a destructured
  subject; and `load, _ = pair` unpacks a container whose contents and length
  the scan does not model. Following any of them means modelling container
  contents or a protocol method -- the same step the bullet above declines --
  so they stay out, and `test_the_guard_admits_the_runtime_binding_forms`
  asserts each one alongside the assignment spelling that *is* caught.
* **A handoff between two scanned files.** Provenance is built from one
  file's own imports, so a value that becomes the importer in file A and is
  used in file B is invisible in both::

      # helper_a.py            load = importlib.import_module   (never called)
      # helper_b.py            from helper_a import load;  load("platform")

  Neither file is flagged: A only assigns, and B has no import that makes
  `load` `importlib`'s. A `PLATFORM_IMPORT_EXEMPT` entry, were one added, could be
  re-exported the same way -- `from tests.<exempt module> import platform` binds the
  module in the importing file under a name that file's syntax says nothing about.
  Earlier rounds of this docstring claimed the opposite, that whichever file
  acquires the module is flagged there so a handoff cannot launder it. That was
  **false**, and it is retracted rather than reworded: neither file acquires the
  module in a form this scan reads. Closing it would take cross-file export
  analysis, which is out of proportion for a test-suite guard and would restart
  the widen-the-scan regress provenance tracking ended. So it stays inside the residue, and
  `test_the_guard_admits_the_cross_file_handoff` asserts it rather than
  describing it.
* **A name with no visible import.** New with provenance tracking, and
  the price of it: a bare `import_module("platform")` in a file that never
  imports it, or `mm["platform"]` where `mm` arrived as a function parameter,
  is not flagged. The scan will not guess that such a name is `importlib`'s or
  `sys.modules`, because guessing is precisely what made it fail on
  `self.modules` and on a fixture. The trade is deliberate: a false positive
  discredits the whole guard, a miss of this shape costs one form that no
  ordinary code writes by accident.
* **Attribute access the scan cannot attribute.** For the same reason,
  `platform` reached through anything but a `Name` -- `mods["sys"].modules`,
  `self.importlib.import_module` -- is invisible. Provenance is tracked for
  simple names only.
* **An expression where the scan expects a name.** The same limit one position
  over, and the one place it surprises. `_bindings` follows the walrus, so
  `(load := importlib.import_module)` does make `load` an importer -- and yet
  calling it inline, `m = (load := ...)("platform")`, reports nothing, because
  acquisition matches a call whose target is a `Name` or a dotted name and this
  one's target is the walrus itself. Split it into the two lines and it is
  flagged. `(sys.modules if c else {})["platform"]` and `(p := platform).system()`
  are the same shape in the map and the module positions. The general form is
  *any* expression in a name's place, and telling the ones that reach an
  importer from the ones that do not means evaluating it; matching the walrus
  alone would catch a spelling and miss the class, which is the move
  provenance tracking stopped. Asserted in `test_the_guard_admits_the_expression_call_target`,
  beside the spelling that is caught.
* **Code assembled at runtime.** `exec`, `eval`, a template rendered to a file
  and imported, a `.pth`. Deliberate rather than missed: `conftest`'s probe
  *is* source text for a child process, and running it there is the fix, so
  string contents must stay legal. A test that built a forbidden call at
  runtime would be defeating this guard on purpose rather than by accident,
  which is the boundary the scan draws and the one it can defend.
* **Files outside the scanned set.** The set is test support -- everything
  under `tests/`, every `conftest.py` including the repository-root one, and
  every `test_*.py` / `*_test.py` elsewhere in the tree. Build scripts are not
  test code and are not scanned.
* **pytest itself.** `_pytest/capture.py:_readline_workaround()` imports
  `readline`, which on Windows is `pyreadline3`, whose `__init__.py:26` runs
  `_S = system()` at import time. Measured: a one-line test file in an empty
  temporary directory, with no part of this repository involved, spawns one
  unsuppressed `ver` before collection begins. No change to this repository
  can remove it. It is recorded here so the next person to instrument a run
  finds the first spawn already explained.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent

#: Every `platform` function measured to reach `_syscmd_ver()` on Windows.
#: `architecture` and `python_version` are absent on purpose: measured at zero
#: spawns, so the reference rule has no reason to name them. That makes them
#: reachable only inside `PLATFORM_IMPORT_EXEMPT`, where the reference rule is
#: the only rule left -- ordinary test support cannot import `platform` at all,
#: so it cannot reach them either.
UNAME_REACHING = frozenset(
    {"platform", "system", "version", "machine", "uname", "win32_ver", "release", "node", "processor"}
)

#: Directory names the walk never descends into: build output, caches, virtual
#: environments, vendored trees. None of them is ModelQuad-owned test code,
#: which is the only thing this guard claims anything about.
PRUNED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "site-packages",
        "venv",
        "worktrees",
    }
)

#: The only test-support modules allowed to bind `platform` itself, each with
#: its reason. Keep this at zero entries if you can and one line if you cannot:
#: every entry is a module where the strong rule is switched off and only the
#: weaker reference rule remains. The exemptions are themselves tested --
#: a stale path or an entry that stopped needing it is a failure, not a
#: comment that quietly went out of date.
#: Empty since `platformcache` and its test (tests/test_windows_facts.py) were
#: removed: no test-support module binds `platform` any more.
PLATFORM_IMPORT_EXEMPT: dict[str, str] = {}


class Offence(NamedTuple):
    """One place a module obtains `platform`, or reaches a spawning name in it.

    ``kind`` is ``"acquire"`` or ``"use"``, because the two rules are enforced
    over different sets of files: acquisition everywhere except
    `PLATFORM_IMPORT_EXEMPT`, use everywhere with no exception at all.
    """

    kind: str
    what: str
    lineno: int


def _is_string(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


#: The two functions that return a module given its name, each paired with the
#: module it has to have come from. Matching the attribute name alone was
#: the scan's false-positive half: a fixture with an ``import_module`` method is
#: not ``importlib``'s, and ``self.modules`` is not ``sys``'s. A spelling only
#: counts once the object it hangs off is traceable back to an ``import``.
IMPORTER_ATTRIBUTES = {"import_module": "importlib", "__import__": "builtins"}

#: ``dict`` accessors on the module map that hand back the module. Subscripting
#: is the obvious one and was covered; these three read the same mapping through
#: a method and were not. ``keys`` and ``__contains__`` are absent
#: deliberately: they answer a question *about* the map without yielding the
#: module, so they acquire nothing.
MODULE_MAP_GETTERS = frozenset({"get", "pop", "setdefault"})

#: The attribute of `sys` that is the module map.
MODULE_MAP_ATTRIBUTE = "modules"


class Provenance(NamedTuple):
    """Which local names in one file hold the objects an acquisition needs.

    The scan used to match ``.modules`` and ``.import_module`` on the
    attribute name alone, which is wrong in both directions at once. It *missed*
    ``from sys import modules as mm`` -- a renamed import of the map, which no
    plain assignment introduces and which the alias pre-pass therefore never
    saw -- and it *flagged* ``self.modules`` and a fixture's ``import_module``,
    neither of which has anything to do with ``sys`` or ``importlib``. Both
    halves are one defect: matching a name instead of an object. A guard that
    cries wolf on unrelated code is deleted by the next person who trips over
    it, so the false positives cost as much as the misses.

    ``holders`` maps a stdlib module name to the local names bound to it, so
    ``import sys as s`` puts ``s`` under ``"sys"``; ``s.modules`` is then the
    module map and ``self.modules`` is not.
    """

    importers: frozenset[str]
    module_maps: frozenset[str]
    holders: dict[str, frozenset[str]]


def _unpacked(target: ast.AST, value: ast.AST) -> list[tuple[str, ast.AST]]:
    """Pair each name a target binds with the expression it binds.

    A ``Name`` target is the whole of it. A tuple or list target is followed
    only against a tuple or list *written in the same statement*, where the
    correspondence is positional and exact: ``load, _ = importlib.import_module,
    None`` is a rename with a comma in it, and nothing about it is runtime
    indirection. One starred target is allowed, because the positions before it
    and the positions after it are still fixed by counting from each end; the
    starred name itself binds a *list* of values rather than any one of them, so
    it is bound to nothing. A starred **value** is refused outright: it splices
    an unknown number of elements, after which no position on either side is
    determined, and guessing is what provenance tracking removed from this scan.

    Unpacking against anything but a display -- ``p, _ = pair`` -- is refused
    for the same reason the residue refuses an importer parked in a dict: it
    means reading a value out of a container the scan does not model.
    """

    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if not (
        isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
    ):
        return []
    values = value.elts
    if any(isinstance(element, ast.Starred) for element in values):
        return []
    targets = target.elts
    stars = [index for index, element in enumerate(targets) if isinstance(element, ast.Starred)]
    if not stars:
        if len(targets) != len(values):
            return []
        pairs = list(zip(targets, values))
    else:
        star = stars[0]
        trailing = len(targets) - star - 1
        if len(stars) > 1 or len(values) < star + trailing:
            return []
        pairs = [
            *zip(targets[:star], values[:star]),
            *zip(targets[star + 1 :], values[len(values) - trailing :]),
        ]
    return [pair for element, bound in pairs for pair in _unpacked(element, bound)]


def _bindings(node: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every ``name = expression`` one node makes, whichever way it spells it.

    Three statements bind an expression written beside the name: plain
    assignment, **annotated** assignment, and the walrus. The annotated form was
    a review finding -- ``load: object = importlib.import_module`` was invisible while the
    identical line without the annotation was flagged, which is a spelling rule
    wearing a guarantee's clothes, the same defect an earlier review found. A bare
    annotation (``load: object``) has no value at all: it binds nothing and must
    not be mistaken for a binding to ``None``.

    What is deliberately *not* here is in the module docstring's residue: a
    ``for``, a ``with``, a comprehension and a ``match`` capture each bind
    something a runtime step produces -- an element pulled out of an iterable,
    ``__enter__``'s return value, a destructured subject -- rather than the
    expression standing next to the name.
    """

    if isinstance(node, ast.Assign):
        return [pair for target in node.targets for pair in _unpacked(target, node.value)]
    if isinstance(node, ast.AnnAssign):
        return [] if node.value is None else _unpacked(node.target, node.value)
    if isinstance(node, ast.NamedExpr):
        return _unpacked(node.target, node.value)
    return []


def _assignment_closure(
    tree: ast.AST, seeds: set[str], holds: Callable[[ast.AST, set[str]], bool]
) -> set[str]:
    """Grow ``seeds`` across the binding forms of `_bindings` until it stops.

    Three sets need this walk -- module holders, importers, module maps -- and
    all three for one reason: a rename is a chain whose length is whatever its
    author felt like (``load = importlib.import_module``, then ``also = load``),
    and the binding may be written below the line that uses it, so a single
    forward pass would not do either.

    **Until it stops, not for a fixed number of passes.** This walk
    used to run at most sixteen times, which is a guess at how long a chain
    anyone writes rather than a property of the problem, and a seventeen-link
    chain written in reverse order -- each alias above the line that defines
    it, so a pass resolves exactly one link -- walked straight out of a guard
    whose docstring says statically visible renames are inside the boundary,
    unqualified by depth. The counter was never needed for safety: ``names``
    only ever grows, and only ever by a name some `_bindings` target in this
    file spells, so it is bounded above by the file's finite set of bound
    names. Every pass therefore either adds one of them or is the last, which
    is the termination argument the counter was standing in for.
    """

    names = set(seeds)
    while True:
        before = len(names)
        for node in ast.walk(tree):
            for name, value in _bindings(node):
                if holds(value, names):
                    names.add(name)
        if len(names) == before:
            return names


def _module_holders(tree: ast.AST, module: str) -> set[str]:
    """Every local name bound to ``module`` itself.

    ``import sys as s`` binds ``s``; ``import importlib.util`` binds
    ``importlib``; ``import importlib.util as u`` binds the *submodule*, so it
    is not a holder of ``importlib`` and ``u.import_module`` is correctly not
    an importer.
    """

    seeds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == module:
                seeds.add(alias.asname or module)
            elif alias.name.startswith(f"{module}.") and alias.asname is None:
                seeds.add(module)
    return _assignment_closure(
        tree, seeds, lambda value, known: isinstance(value, ast.Name) and value.id in known
    )


def _holds_importer(node: ast.AST, importers: set[str], holders: dict[str, frozenset[str]]) -> bool:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        home = IMPORTER_ATTRIBUTES.get(node.attr)
        return home is not None and node.value.id in holders.get(home, frozenset())
    return isinstance(node, ast.Name) and node.id in importers


def _holds_module_map(
    node: ast.AST, module_maps: set[str], holders: dict[str, frozenset[str]]
) -> bool:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.attr == MODULE_MAP_ATTRIBUTE and node.value.id in holders.get("sys", frozenset())
    return isinstance(node, ast.Name) and node.id in module_maps


def _provenance(tree: ast.AST) -> Provenance:
    """Read one file's imports, then follow every rename they can reach.

    Seeded from import statements only -- that is what makes this provenance
    rather than name matching -- and then closed over assignments, because
    ``from importlib import import_module as load`` and ``load =
    importlib.import_module`` are one act written two ways.
    """

    holders = {
        name: frozenset(_module_holders(tree, name)) for name in ("sys", "importlib", "builtins")
    }

    # `__import__` is a builtin: it is itself, under its own name, with no
    # import statement anywhere. Every other seed has to be earned.
    importer_seeds = {"__import__"}
    map_seeds: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        for alias in node.names:
            if alias.name == "*":
                # A star import binds every public name of the module, so it
                # binds these too -- but not the underscored ones, which is why
                # `from builtins import *` does not bring in `__import__`.
                importer_seeds.update(
                    attribute
                    for attribute, home in IMPORTER_ATTRIBUTES.items()
                    if home == node.module and not attribute.startswith("_")
                )
                if node.module == "sys":
                    map_seeds.add(MODULE_MAP_ATTRIBUTE)
            elif IMPORTER_ATTRIBUTES.get(alias.name) == node.module:
                importer_seeds.add(alias.asname or alias.name)
            elif node.module == "sys" and alias.name == MODULE_MAP_ATTRIBUTE:
                map_seeds.add(alias.asname or alias.name)

    return Provenance(
        importers=frozenset(
            _assignment_closure(
                tree, importer_seeds, lambda value, known: _holds_importer(value, known, holders)
            )
        ),
        module_maps=frozenset(
            _assignment_closure(
                tree, map_seeds, lambda value, known: _holds_module_map(value, known, holders)
            )
        ),
        holders=holders,
    )


def _is_importer(node: ast.AST, provenance: Provenance) -> bool:
    """Does this expression *evaluate to* an import function, rather than call one?

    Used twice: to grow ``Provenance.importers`` along an assignment chain, and
    to decide whether a call is a dynamic import. Keeping it one predicate is
    what makes ``load = importlib.import_module`` and ``load("platform")``
    agree.
    """

    return _holds_importer(node, provenance.importers, provenance.holders)


def _names_modules(node: ast.AST, provenance: Provenance) -> bool:
    """``sys.modules``, under whatever name this file actually gave it.

    ``sys.modules``; ``s.modules`` after ``import sys as s``; ``mm`` after
    either ``mm = sys.modules`` or ``from sys import modules as mm`` -- the
    second of which a review found missing, because a renamed *import* of the map
    is not an assignment and the alias pre-pass only read assignments. Not
    ``self.modules``: same attribute name, unrelated object.
    """

    return _holds_module_map(node, provenance.module_maps, provenance.holders)


def _module_name_argument(node: ast.Call) -> ast.AST | None:
    """The argument naming the module, positional or keyword.

    ``importlib.import_module(name="platform")`` was the second miss: both
    importers take the module name as ``name``, and requiring it positionally
    made the keyword spelling -- legal, ordinary, and doing exactly the same
    thing -- invisible.
    """

    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "name":
            return keyword.value
    return None


def _dynamic_import(node: ast.AST, provenance: Provenance) -> str | None:
    """``importlib.import_module("platform")`` / ``__import__("platform")``.

    Matched through `_is_importer`, so the dotted spelling, a bare ``from
    importlib import import_module``, and a rename to anything at all are one
    rule -- and an unrelated object's ``import_module`` is none of them. The
    module name still has to be a literal, because a computed one is
    runtime-assembled code, which the module docstring places outside what this
    scan defends rather than pretending to cover.
    """

    if not isinstance(node, ast.Call):
        return None
    named = _module_name_argument(node)
    if named is None or not _is_string(named, "platform"):
        return None
    if not _is_importer(node.func, provenance):
        return None
    func = node.func
    spelled = func.attr if isinstance(func, ast.Attribute) else func.id
    return f"{spelled}('platform')"


def _module_map_access(node: ast.AST, provenance: Provenance) -> str | None:
    """``sys.modules["platform"]`` and its method spellings -- import-free acquisition.

    Subscripting was covered from the start; ``sys.modules.get("platform")``
    was not, and is the third hole a review found. It reads the same mapping and
    returns the same module object -- and is arguably the *more* natural
    spelling, because it tolerates the module being absent.
    """

    if isinstance(node, ast.Subscript) and _is_string(node.slice, "platform"):
        return "sys.modules['platform']" if _names_modules(node.value, provenance) else None
    if (
        isinstance(node, ast.Call)
        and node.args
        and _is_string(node.args[0], "platform")
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in MODULE_MAP_GETTERS
        and _names_modules(node.func.value, provenance)
    ):
        return f"sys.modules.{node.func.attr}('platform')"
    return None


def _platform_offences(tree: ast.AST) -> list[Offence]:
    """Every acquisition of `platform`, and every reference to a spawning name.

    Four passes, in this order because each needs the one before it:

    0. **Provenance.** Which local names hold `sys`, `importlib`, the module
       map and an importer, read from this file's import statements and then
       followed across renames. Passes 1 and 2 ask it what a spelling means,
       rather than deciding from the attribute name.
    1. **Acquisition.** The five ways to obtain the module. Each is reported,
       and each binds a local name the third pass then treats as the module.
    2. **Rebinding, to a fixed point.** ``p = platform``, ``q = p``: an alias
       chain is free to rewrite, so following it is not optional. Across every
       binding form `_bindings` reads, and no others -- the module alias, the
       importer and the module map are three call sites of one definition of
       *rename*, which is what a review found them disagreeing about.
    3. **Reference.** Any attribute of a bound module name that is in
       `UNAME_REACHING`, whether or not it is called; ``getattr`` with a
       literal name, likewise; and a bare name a ``from platform import``
       brought in. Reporting the *reference* rather than the *call* is what
       makes ``f = platform.system`` fail on its own line -- the form a
       call-site scan cannot follow.

    A call written inside a string literal is invisible to ``ast`` and stays
    legal on purpose: ``conftest``'s probe is source code for a *child*
    process, and running it there is the fix, not the offence.
    """

    module_aliases: set[str] = set()
    bare_names: dict[str, str] = {}
    star_imported = False
    offences: dict[tuple[int, str], Offence] = {}
    provenance = _provenance(tree)

    def record(kind: str, what: str, lineno: int) -> None:
        offences.setdefault((lineno, what), Offence(kind, what, lineno))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "platform" or alias.name.startswith("platform."):
                    module_aliases.add(alias.asname or alias.name.split(".")[0])
                    spelled = alias.name + (f" as {alias.asname}" if alias.asname else "")
                    record("acquire", f"import {spelled}", node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "platform" and not node.level:
            names = ", ".join(alias.name for alias in node.names)
            record("acquire", f"from platform import {names}", node.lineno)
            for alias in node.names:
                if alias.name == "*":
                    star_imported = True
                elif alias.name in UNAME_REACHING:
                    bare_names[alias.asname or alias.name] = alias.name
        elif (dynamic := _dynamic_import(node, provenance)) is not None:
            record("acquire", dynamic, node.lineno)
        elif (mapped := _module_map_access(node, provenance)) is not None:
            record("acquire", mapped, node.lineno)

    # An alias chain is as long as whoever wrote it wanted, so this runs until
    # the set stops growing rather than for a fixed number of passes,
    # on the same termination argument as `_assignment_closure`: the set only
    # grows, and only by names this file binds.
    while True:
        before = len(module_aliases)
        for node in ast.walk(tree):
            for name, value in _bindings(node):
                holds_module = (
                    (isinstance(value, ast.Name) and value.id in module_aliases)
                    or _dynamic_import(value, provenance) is not None
                    or _module_map_access(value, provenance) is not None
                )
                if holds_module:
                    module_aliases.add(name)
        if len(module_aliases) == before:
            break

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            and node.attr in UNAME_REACHING
        ):
            record("use", f"platform.{node.attr}", node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in module_aliases
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in UNAME_REACHING
        ):
            record("use", f"getattr(platform, {node.args[1].value!r})", node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in bare_names:
                record("use", f"platform.{bare_names[node.id]} [imported bare]", node.lineno)
            elif star_imported and node.id in UNAME_REACHING:
                record("use", f"platform.{node.id} [star import]", node.lineno)

    return sorted(offences.values(), key=lambda offence: (offence.lineno, offence.what))


def _scanned_files() -> list[Path]:
    """Every ModelQuad-owned module that runs as test support.

    Wider than `tests/` on purpose. The repository-root `conftest.py`
    is loaded by pytest on **every** session -- `pyproject.toml` pins `rootdir`
    here, so `pytest`, `python -m pytest` and a run started from inside
    `tests/` all import it before collection -- and a scan claiming *no
    ModelQuad-owned test code triggers this path* while skipping the one file
    that always runs was claiming more than it checked. Any other
    `conftest.py`, and any `test_*.py` / `*_test.py` elsewhere in the tree,
    are in the set for the same reason.

    Walked with pruning rather than `rglob`, so a `build/` tree or a virtual
    environment in the working copy costs nothing and contributes nothing.
    """

    found: list[Path] = []
    stack = [REPO_ROOT]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if entry.is_dir():
                if entry.name not in PRUNED_DIRECTORIES:
                    stack.append(entry)
                continue
            if entry.suffix != ".py":
                continue
            relative = entry.relative_to(REPO_ROOT)
            if (
                relative.parts[0] == "tests"
                or entry.name == "conftest.py"
                or entry.name.startswith("test_")
                or entry.name.endswith("_test.py")
            ):
                found.append(entry)
    return sorted(found)


@pytest.mark.ci_only
def test_no_test_module_calls_platform_in_process():
    """The guard. Fails the moment the in-process call returns to test support.

    Written as a scan rather than a fix to one line because the deletion that
    closed that finding protects nothing on its own: the next test that wants to know
    what OS it is on will reach for ``platform.system()``, it will read as
    obviously harmless, and on a developer's machine with a warm
    ``_uname_cache`` it will even pass silently. This says no once, for the
    whole set, and names the replacement in the failure message.
    """

    acquisitions: list[str] = []
    uses: list[str] = []
    scanned = _scanned_files()
    for path in scanned:
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for offence in _platform_offences(tree):
            if offence.kind == "acquire" and relative in PLATFORM_IMPORT_EXEMPT:
                continue
            where = f"{relative}:{offence.lineno}: {offence.what}"
            (acquisitions if offence.kind == "acquire" else uses).append(where)

    assert len(scanned) > 1, "the scan found no test modules; it is measuring nothing"
    assert not uses, (
        "these reach a `platform` function measured to spawn `cmd /c ver` on "
        "Windows from a cold `platform._uname_cache`, which is the popup "
        "this guard exists to prevent -- use `conftest.platform_reference()`, "
        f"which reads the same values in a CREATE_NO_WINDOW child: {uses}"
    )
    assert not acquisitions, (
        "test-support code may not obtain the `platform` module at all -- every "
        "spawning call needs it first, and banning the acquisition is the only "
        "rule an alias, a `getattr` or a star import cannot walk around. Read "
        "what `platform` reports through `conftest.platform_reference()`; if a "
        "module genuinely needs the module object itself, add it to "
        f"PLATFORM_IMPORT_EXEMPT with the reason: {acquisitions}"
    )


def test_the_scan_reaches_test_support_code_outside_the_tests_directory():
    """A scan of the wrong set is a scan of nothing.

    Half of that finding was not that the matching was wrong but that the *set*
    was: the repository-root ``conftest.py`` runs on every session and was
    never read. Naming it here makes dropping it a test failure rather than a
    silent narrowing.
    """

    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _scanned_files()}

    assert "conftest.py" in scanned, "the root conftest runs on every pytest session"
    assert "tests/conftest.py" in scanned
    assert len([name for name in scanned if name.startswith("tests/")]) > 1
    assert not [name for name in scanned if "__pycache__" in name or name.startswith("build/")]


def test_the_import_prohibition_is_exempted_only_where_it_is_earned():
    """Each exemption is a real file, and the second rule still holds inside it.

    An exemption list is where a guard goes to die: a stale entry keeps a rule
    switched off for a file that no longer exists or no longer needs it, and an
    entry with no remaining rule inside it is a hole. Both are checked here, so
    the list cannot rot quietly.
    """

    for relative, reason in PLATFORM_IMPORT_EXEMPT.items():
        path = REPO_ROOT / relative
        assert path.is_file(), f"exempt path no longer exists: {relative}"
        assert len(reason) > 40, f"an exemption needs a reason, not a label: {relative}"
        offences = _platform_offences(ast.parse(path.read_text(encoding="utf-8")))
        assert [o for o in offences if o.kind == "acquire"], (
            f"{relative} no longer imports `platform`; delete the exemption"
        )
        assert not [o for o in offences if o.kind == "use"], (
            f"{relative} is exempt from the acquisition rule, not from the call rule: "
            f"{[o.what for o in offences if o.kind == 'use']}"
        )


def test_the_guard_detects_both_import_spellings():
    """The scan is proved on source it is handed, not trusted to be non-empty.

    A scan whose only evidence is that it currently finds nothing is
    indistinguishable from a scan with a typo'd attribute name. These snippets
    are the cases that decide whether the test above means anything: the two
    ways to spell the call, the alias that would dodge a name-matching scan,
    and the string literal that must *not* be flagged so ``conftest``'s
    hidden-child probe stays legal.
    """

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    assert uses("import platform\nplatform.system()\n") == ["platform.system"]
    assert uses("import platform as p\np.platform()\n") == ["platform.platform"]
    assert uses("from platform import version\nversion()\n") == [
        "platform.version [imported bare]"
    ]
    # Measured at zero spawns, so the reference rule passes over them -- and
    # that is the whole of it. The acquisition rule still rejects the
    # import on the line above, so this snippet is an offending file; the two
    # functions are only reachable where an exemption has switched that rule
    # off. Both halves are asserted, because the `uses` half read alone is what
    # let the docstring claim they were callable anywhere.
    spawn_free = "import platform\nplatform.python_version()\nplatform.architecture()\n"
    assert uses(spawn_free) == []
    assert [
        o.what for o in _platform_offences(ast.parse(spawn_free)) if o.kind == "acquire"
    ] == ["import platform"]
    # Allowed: source for a child process, which is the approved mechanism.
    assert uses("probe = 'import platform; platform.platform()'\n") == []


def test_the_guard_detects_the_indirect_forms_review_r5_f2_named():
    """Guard-the-guard for every form the call-site-only scan let through.

    Each snippet reaches a spawning ``platform`` function and the previous
    version of ``_platform_offences`` returned nothing for it. They are
    ordinary Python, not obfuscation, which is the force of the finding --
    someone writing ``f = platform.system`` is not evading a rule, they are
    factoring out a lookup, and the popup arrives anyway.

    Read the two halves together. The *use* rule catches the reference wherever
    it is spelled; the *acquire* rule catches the module arriving in the first
    place. A form that slips past one is still stopped by the other, and the
    last case here is one that only the acquisition rule sees.
    """

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # A callable alias. Flagged at the assignment, so where `f` is finally
    # called -- another module, a fixture, a dict of callbacks -- neither
    # matters nor has to be traced.
    assert uses("import platform\nf = platform.system\nf()\n") == ["platform.system"]
    # The same lookup through a string, which no name-matching scan sees.
    assert uses('import platform\ngetattr(platform, "system")()\n') == [
        "getattr(platform, 'system')"
    ]
    assert uses('import platform\nf = getattr(platform, "system")\nf()\n') == [
        "getattr(platform, 'system')"
    ]
    # A star import binds every name without naming one.
    assert acquisitions("from platform import *\n") == ["from platform import *"]
    assert uses("from platform import *\nsystem()\n") == ["platform.system [star import]"]
    # Aliasing the module itself, one hop or several.
    assert uses("import platform\np = platform\nq = p\nq.uname()\n") == ["platform.uname"]
    # Obtaining the module with no import statement at all.
    assert acquisitions('import importlib\nm = importlib.import_module("platform")\n') == [
        "import_module('platform')"
    ]
    assert uses('import importlib\nm = importlib.import_module("platform")\nm.release()\n') == [
        "platform.release"
    ]
    assert acquisitions('m = __import__("platform")\n') == ["__import__('platform')"]
    assert uses('import sys\nm = sys.modules["platform"]\nm.node()\n') == ["platform.node"]
    # Obtained and used in one expression, binding no name: the reference rule
    # follows names and sees nothing here, and the acquisition rule is what
    # stops it. This is the layering doing the work rather than decorating it.
    assert uses('import sys\nsys.modules["platform"].node()\n') == []
    assert acquisitions('import sys\nsys.modules["platform"].node()\n') == [
        "sys.modules['platform']"
    ]
    # The rule that makes all of the above redundant in ordinary code: the
    # module may not be obtained, so there is nothing left to alias.
    assert acquisitions("import platform\n") == ["import platform"]
    assert acquisitions("import platform as p\n") == ["import platform as p"]
    assert acquisitions("from platform import system\n") == ["from platform import system"]
    # Still not an offence: a string is source for a child, which is the fix.
    assert _platform_offences(ast.parse("probe = 'from platform import *; system()'\n")) == []


def test_the_guard_detects_the_renamed_acquisitions_review_r6_f2_named():
    """Guard-the-guard for the three spellings a review measured as unflagged.

    Each of these acquires `platform` and the previous `_platform_offences`
    returned *nothing at all* for it -- not a weaker offence, none. Worse than
    the finding reported: because the bound name never became a tracked module
    alias, the `use` rule went quiet too, so `m.system()` on the next line was
    invisible as well. Both halves are asserted here.

    The forms are deliberately dull. `from importlib import import_module as
    load` is what someone writes to shorten a long name; `mm = sys.modules` is
    what someone writes before touching it twice. That is the whole argument
    for widening the scan rather than narrowing its docstring -- see the module
    docstring, and the residue bullet that says where the following stops.
    """

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # 1. The importer renamed at the import.
    assert acquisitions('from importlib import import_module as load\nm = load("platform")\n') == [
        "load('platform')"
    ]
    assert uses(
        'from importlib import import_module as load\nm = load("platform")\nm.system()\n'
    ) == ["platform.system"]

    # 2. The importer renamed by assignment, and along a chain.
    assert acquisitions('import importlib\nload = importlib.import_module\nload("platform")\n') == [
        "load('platform')"
    ]
    assert uses(
        'import importlib\nload = importlib.import_module\nalso = load\n'
        'm = also("platform")\nm.node()\n'
    ) == ["platform.node"]
    # The binding may be written below its use; the pre-pass is why that holds.
    assert acquisitions(
        'm = load("platform")\nfrom importlib import import_module as load\n'
    ) == ["load('platform')"]

    # 3. The module map read through a method rather than a subscript.
    assert acquisitions('import sys\nm = sys.modules.get("platform")\n') == [
        "sys.modules.get('platform')"
    ]
    assert uses('import sys\nm = sys.modules.get("platform")\nm.system()\n') == [
        "platform.system"
    ]
    assert acquisitions('import sys\nsys.modules.pop("platform")\n') == [
        "sys.modules.pop('platform')"
    ]
    assert acquisitions('from sys import modules\nm = modules.get("platform")\n') == [
        "sys.modules.get('platform')"
    ]

    # 3b. The aliased map, which the older subscript rule missed too.
    assert uses('import sys\nmm = sys.modules\nm = mm["platform"]\nm.release()\n') == [
        "platform.release"
    ]
    assert acquisitions('import sys\nmm = sys.modules\nalso = mm\nm = also.get("platform")\n') == [
        "sys.modules.get('platform')"
    ]

    # Not offences, and the distinction is the point: these read the map
    # without taking the module out of it, so nothing is acquired.
    assert _platform_offences(ast.parse('import sys\nif "platform" in sys.modules:\n    pass\n')) == []
    assert _platform_offences(ast.parse("import sys\nprint(sys.modules.keys())\n")) == []
    # Nor is an unrelated mapping that happens to be asked about "platform".
    assert _platform_offences(ast.parse('registry = {}\nx = registry.get("platform")\n')) == []


def test_the_guard_tracks_provenance_rather_than_attribute_names_review_r7_f1_named():
    """Guard-the-guard for the attribute-name scan, which was wrong in both directions at once.

    Two forms were missed and three unrelated ones were flagged, and the second
    half matters as much as the first: a guard that fails on `self.modules` is
    a guard the next person deletes, and then the misses stop mattering because
    nothing is checked at all.

    Both halves come from the same replacement. `Provenance` seeds its sets from
    *import statements* and follows renames from there, so `.modules` counts
    only on a name bound to `sys` and `.import_module` only on one bound to
    `importlib`. That single change sees the renamed import it used to miss and
    stops guessing about names it never saw imported.
    """

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # 1. Missed: the module map renamed at its *import*. `_module_map_aliases`
    #    read assignments only, so `mm` was never a map and `mm["platform"]`
    #    was not an acquisition -- which also left `m.system()` unseen.
    assert acquisitions('from sys import modules as mm\nm = mm["platform"]\n') == [
        "sys.modules['platform']"
    ]
    assert uses('from sys import modules as mm\nm = mm["platform"]\nm.system()\n') == [
        "platform.system"
    ]
    assert acquisitions('from sys import modules as mm\nalso = mm\nalso.get("platform")\n') == [
        "sys.modules.get('platform')"
    ]

    # 2. Missed: the module named by keyword. Both importers spell the
    #    parameter `name`, and the scan required it positionally.
    assert acquisitions('import importlib\nimportlib.import_module(name="platform")\n') == [
        "import_module('platform')"
    ]
    assert uses(
        'import importlib\nm = importlib.import_module(name="platform")\nm.node()\n'
    ) == ["platform.node"]
    assert acquisitions('__import__(name="platform")\n') == ["__import__('platform')"]

    # 3. False positives: the same attribute names on objects that are not
    #    `sys` and not `importlib`. Each of these was reported as an
    #    acquisition before provenance, on code with no `platform` in it.
    assert _platform_offences(
        ast.parse('class C:\n    def f(self):\n        return self.modules["platform"]\n')
    ) == []
    assert _platform_offences(
        ast.parse('def test_x(fixture):\n    return fixture.import_module("platform")\n')
    ) == []
    assert _platform_offences(ast.parse('modules = {}\nx = modules.get("platform")\n')) == []
    assert _platform_offences(
        ast.parse('def test_x(registry):\n    registry.modules.get("platform")\n')
    ) == []

    # The distinction those three rest on: the *same* spellings, with the import
    # that makes them mean what the guard thought they meant.
    assert acquisitions('import sys\nsys.modules.get("platform")\n') == [
        "sys.modules.get('platform')"
    ]
    assert acquisitions('import sys as s\ns.modules["platform"]\n') == ["sys.modules['platform']"]
    assert acquisitions('import builtins\nbuiltins.__import__("platform")\n') == [
        "__import__('platform')"
    ]
    # A submodule import binds the submodule under an `as`, and the package
    # without one -- so `u` is not `importlib` and plain `importlib` is.
    assert acquisitions('import importlib.util as u\nu.import_module("platform")\n') == []
    assert acquisitions('import importlib.util\nimportlib.import_module("platform")\n') == [
        "import_module('platform')"
    ]
    # A star import is provenance too, and the underscore rule is Python's.
    assert acquisitions('from importlib import *\nimport_module("platform")\n') == [
        "import_module('platform')"
    ]
    assert acquisitions('from sys import *\nm = modules["platform"]\n') == [
        "sys.modules['platform']"
    ]


def test_the_guard_follows_the_other_binding_forms_review_r9_f1_named():
    """Guard-the-guard for a review finding: the same rename, spelled three other ways.

    The finding was one line -- ``load: object = importlib.import_module``
    returned *nothing at all*, while the identical line without the annotation
    was flagged. An annotation is not evasion; it is what a typed codebase
    writes, and a guard that stops the untyped spelling and waves the typed one
    through is a spelling rule wearing a guarantee's clothes, which is exactly
    the argument an earlier review already settled once.

    Reviewing the closure for the rest of its blind spots found two more of the
    same shape -- the walrus, and unpacking against a display -- and four that
    are genuinely different in kind and stayed out. Those four are the residue,
    asserted in `test_the_guard_admits_the_runtime_binding_forms`; these three
    bind an expression written next to the name, so they are inside the
    boundary the guarantee already draws and they are followed.

    Each case is asserted through the three sets the closure feeds -- the
    importer, the module map, the module alias -- because they are three call
    sites of one walk and a fix that reached only one of them would be a fourth
    round of this.
    """

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # 1. The annotated assignment: the finding itself, in all three sets.
    assert acquisitions(
        'import importlib\nload: object = importlib.import_module\nm = load("platform")\n'
    ) == ["load('platform')"]
    assert uses(
        'import importlib\nload: object = importlib.import_module\n'
        'module = load("platform")\nmodule.system()\n'
    ) == ["platform.system"]
    assert uses('import sys\nmm: dict = sys.modules\nm = mm["platform"]\nm.node()\n') == [
        "platform.node"
    ]
    assert uses("import platform\np: object = platform\nq = p\nq.release()\n") == [
        "platform.release"
    ]
    # A *bare* annotation has no value: it binds nothing, and reading `.value`
    # off it without checking would be an `AttributeError` on ordinary source.
    assert _platform_offences(ast.parse("load: object\nplatform: object\n")) == []
    assert acquisitions('load: object\nimport importlib\nload = importlib.import_module\n'
                        'm = load("platform")\n') == ["load('platform')"]

    # 2. The walrus, which binds in an expression rather than a statement.
    assert acquisitions(
        'import importlib\nif (load := importlib.import_module):\n    load("platform")\n'
    ) == ["load('platform')"]
    assert uses("import platform\nif (p := platform):\n    p.uname()\n") == ["platform.uname"]

    # 3. Unpacking against a display, where the correspondence is positional
    #    and written out: one comma away from the plain assignment above.
    assert acquisitions(
        'import importlib\nload, _ = importlib.import_module, None\nload("platform")\n'
    ) == ["load('platform')"]
    assert uses("import platform\np, _ = platform, None\np.system()\n") == ["platform.system"]
    assert uses("import platform\n[p, q] = [None, platform]\nq.node()\n") == ["platform.node"]
    assert uses("import sys\nmm, _ = (sys.modules, None)\nmm['platform'].system()\n") == []
    assert acquisitions("import sys\nmm, _ = (sys.modules, None)\nmm['platform']\n") == [
        "sys.modules['platform']"
    ]
    # A starred target still fixes every other position, by counting from
    # whichever end it is not on. It binds a list itself, so it binds nothing.
    assert uses("import platform\np, *rest = platform, None, None\np.version()\n") == [
        "platform.version"
    ]
    assert uses("import platform\n*rest, p = None, platform\np.machine()\n") == [
        "platform.machine"
    ]

    # An exemption is where these matter most: inside it the acquisition
    # is forgiven and only the reference rule is left, so a missed alias there
    # would be a hole with no second layer under it.
    assert uses("import platform\nshim: object = platform\nf = shim.system\n") == [
        "platform.system"
    ]


def test_the_guard_follows_a_chain_longer_than_any_fixed_cap_review_r10_f1_named():
    """Guard-the-guard: depth is not part of the guarantee.

    Both fixed-point walks used to run ``for _ in range(16)``. Sixteen is a
    guess at how long a rename chain anyone writes, not a property of the
    problem, and the docstring makes no such qualification -- it says a
    statically visible rename is inside the boundary, full stop. The shape that
    collected the difference is a chain written in reverse: each alias defined
    *below* the line that uses it, so a top-down pass resolves exactly one link
    and a chain of ``n`` links needs ``n`` passes. Past the cap it left the tree
    with no offence at all.

    Asserted through all four call sites of the two walks -- the module alias,
    the importer, the module map, and the holder of ``importlib`` itself --
    because they are separate loops over separate sets and a fix that reached
    only one of them would be another round of this. ``DEEP`` is set well past
    the old cap, so reinstating a counter at any value near it fails here.
    """

    DEEP = 32

    def reverse_chain(prefix: str, tail: str) -> str:
        """``x0 = x1`` ... ``xDEEP = tail``, every link above its definition."""

        links = [f"{prefix}{index} = {prefix}{index + 1}" for index in range(DEEP)]
        return "\n".join([*links, f"{prefix}{DEEP} = {tail}", ""])

    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # 1. The module alias, closed in `_platform_offences` itself.
    assert uses("import platform\n" + reverse_chain("p", "platform") + "p0.system()\n") == [
        "platform.system"
    ]

    # 2. The importer, closed in `_assignment_closure` under `_holds_importer`.
    importer = "import importlib\n" + reverse_chain("load", "importlib.import_module")
    assert acquisitions(importer + 'm = load0("platform")\n') == ["load0('platform')"]
    assert uses(importer + 'm = load0("platform")\nm.node()\n') == ["platform.node"]

    # 3. The module map, closed in `_assignment_closure` under `_holds_module_map`.
    mapped = "import sys\n" + reverse_chain("mm", "sys.modules")
    assert acquisitions(mapped + 'm = mm0["platform"]\n') == ["sys.modules['platform']"]
    assert uses(mapped + 'm = mm0["platform"]\nm.version()\n') == ["platform.version"]

    # 4. The holder of `importlib`, which `_module_holders` closes over the same
    #    walk -- an importer is only an importer once the name it hangs off is
    #    traceable back to an import, and that name renames too.
    held = "import importlib\n" + reverse_chain("h", "importlib")
    assert acquisitions(held + 'm = h0.import_module("platform")\n') == [
        "import_module('platform')"
    ]
    assert uses(held + 'm = h0.import_module("platform")\nm.machine()\n') == [
        "platform.machine"
    ]

    # With the counter gone the walks stop only when the set stops growing, so
    # the case that would spin forever if that were wrong is asserted here: a
    # cycle adds nothing on its first pass, and the first pass is the last.
    assert _platform_offences(ast.parse("a = b\nb = a\nload = load\n")) == []


def test_the_guard_admits_where_it_stops_following_a_value():
    """The residue, asserted rather than described.

    A guard's docstring saying "arbitrary runtime indirection is out of scope"
    is worth nothing if nobody ever checked which side of that line a given
    form falls on. These are the forms the module docstring's residue bullet
    claims pass, and they do -- so if a later change starts catching one, this
    test fails and the bullet gets narrowed instead of quietly overpromising in
    the other direction.
    """

    def acquisitions(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "acquire"]

    # A computed module name: not a literal, so not matched.
    assert acquisitions('import importlib\nimportlib.import_module("plat" + "form")\n') == []
    # A name with no visible import. New with provenance tracking, and
    # the price of it: the scan will not guess that a bare `import_module` is
    # `importlib`'s, because guessing is exactly what flagged an unrelated
    # fixture. Any of the real spellings puts it back inside the rule.
    assert acquisitions('import_module("platform")\n') == []
    assert acquisitions('def test_x(mm):\n    return mm["platform"]\n') == []
    # Provenance is tracked for simple names, so `sys` reached through anything
    # else is not `sys` as far as this scan is concerned.
    assert acquisitions('import sys\nmods = {"s": sys}\nmods["s"].modules["platform"]\n') == []
    # The importer parked somewhere the scan does not follow.
    assert acquisitions('import importlib\nfns = {"i": importlib.import_module}\nfns["i"]("platform")\n') == []
    assert acquisitions(
        'import functools, importlib\n'
        'load = functools.partial(importlib.import_module, "platform")\nload()\n'
    ) == []


def test_the_guard_admits_the_runtime_binding_forms():
    """The residue's other half: bindings whose value is produced, not written.

    A review asked which *other* binding forms the closure fails to follow, and the
    answer splits in two. Three of them -- the annotated assignment, the walrus
    and unpacking against a display -- bind an expression standing next to the
    name, so they were added to the scan and are asserted in
    `test_the_guard_follows_the_other_binding_forms_review_r9_f1_named`. These
    are the rest, and each one binds something a runtime step *makes*:

    * ``for p in (platform,)`` binds an element pulled out of an iterable, and
      the loop may bind a different object on each pass;
    * ``with ctx as p`` binds ``__enter__``'s return value, which is by
      definition a different object from the expression written;
    * a comprehension is the ``for`` case with different punctuation;
    * ``match subject: case p:`` binds a destructured subject;
    * unpacking against a name (``p, _ = pair``) or across a starred *value*
      (``p, q = *xs, None``) reads out of a container whose contents and length
      the scan does not model.

    Following any of those means modelling container contents or a protocol
    method -- the same step the residue already declines for an importer parked
    in a dict, and the step whose absence is what keeps this scan from
    guessing. Drawing the line at *the expression is written beside the name*
    is what makes the boundary statable rather than a list of forms that grows
    by one per review round.

    Asserted rather than described, and asserted **for the right reason**: each
    case is followed by the same lines rewritten in a form the scan does cover,
    which is caught. So these pass because the binding is not one the closure
    reads, not because the acquisition spelling went unrecognised.
    """

    def offences(source: str) -> list[str]:
        return [f"{o.kind}:{o.what}" for o in _platform_offences(ast.parse(source))]

    # `for`, and the same acquisition assigned instead.
    assert offences('import importlib\nfor load in (importlib.import_module,):\n'
                    '    m = load("platform")\n    m.system()\n') == []
    assert offences('import importlib\nload = importlib.import_module\n'
                    'm = load("platform")\nm.system()\n') == [
        "acquire:load('platform')",
        "use:platform.system",
    ]

    # `with`: `__enter__` returns whatever it likes, so this is not a rename.
    assert offences('import contextlib, importlib\n'
                    'with contextlib.nullcontext(importlib.import_module) as load:\n'
                    '    load("platform")\n') == []

    # A comprehension is the `for` case with different punctuation.
    assert offences('import importlib\n'
                    '[load("platform") for load in (importlib.import_module,)]\n') == []

    # A `match` capture pattern binds the subject, but through a destructure.
    assert offences('import importlib\nmatch importlib.import_module:\n'
                    '    case load:\n        load("platform")\n') == []

    # Unpacking that is not against a display: the container is not modelled.
    assert offences('import importlib\npair = (importlib.import_module, None)\n'
                    'load, _ = pair\nload("platform")\n') == []
    assert offences('import importlib\nload, _ = *xs, importlib.import_module\n'
                    'load("platform")\n') == []
    # Nor is one whose two sides cannot correspond position for position.
    assert offences('import importlib\nload, other = (importlib.import_module,)\n'
                    'load("platform")\n') == []
    # ...while the display of matching length, one character away, is followed.
    assert offences('import importlib\nload, other = importlib.import_module, None\n'
                    'load("platform")\n') == ["acquire:load('platform')"]

    # The module alias goes the same way, and for the same reason. Each of
    # these is `import platform` too, so read the *use* half: it is absent
    # here and present in the assignment spelling underneath.
    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    assert uses("import platform\nfor p in (platform,):\n    p.system()\n") == []
    assert uses("import platform\np = platform\np.system()\n") == ["platform.system"]


def test_the_guard_admits_the_expression_call_target():
    """The residue one position over: an expression where a name is expected.

    A review fix made `_bindings` follow the walrus, so `(load :=
    importlib.import_module)` really does make `load` an importer, and
    `load("platform")` on the next line is flagged. Calling it *inline* is not:
    acquisition matches a call whose target is a `Name` or a dotted name, and
    here the target is the walrus itself. The supervisor found that gap while
    verifying that fix, and it stays open on purpose.

    On purpose because the general form is any expression in a name's place.
    The conditional below is written out, one branch of it is the importer, and
    it is still not something a static rule resolves without evaluating it.
    Recognising the walrus alone would catch that spelling and miss the class,
    which is exactly the widen-one-form-per-round move provenance tracking
    ended. It is the
    same boundary as `mods["sys"].modules` in the residue above, moved from the
    object position to the callee position.

    Asserted in the shape `test_the_guard_admits_the_runtime_binding_forms`
    uses: every form that passes is followed by the nearest spelling the scan
    *does* cover, which is caught -- so these pass because the position is one
    the scan does not read, not because the acquisition became unrecognisable.
    """

    def offences(source: str) -> list[str]:
        return [f"{o.kind}:{o.what}" for o in _platform_offences(ast.parse(source))]

    # The walrus called inline: the binding is followed, the call site is not,
    # so neither the acquisition nor the use downstream of it is reported.
    assert offences('import importlib\n'
                    'm = (load := importlib.import_module)("platform")\nm.system()\n') == []
    # ...and that is the call target's doing rather than the walrus's: give
    # `load` a line of its own and the binding fix carries it, both halves.
    assert offences('import importlib\nif (load := importlib.import_module):\n'
                    '    m = load("platform")\n    m.system()\n') == [
        "acquire:load('platform')",
        "use:platform.system",
    ]

    # The conditional, which is why the walrus is not special-cased. Both
    # branches are written out and one of them is the importer; the scan still
    # declines, because it does not evaluate call targets.
    assert offences('import importlib\ndef test_x(c):\n'
                    '    m = (importlib.import_module if c else None)("platform")\n'
                    '    m.system()\n') == []
    # Parentheses are not the boundary -- `ast` has no node for them, so a
    # parenthesised name is just the name, and is caught.
    assert offences('import importlib\nload = importlib.import_module\n'
                    'm = (load)("platform")\nm.system()\n') == [
        "acquire:load('platform')",
        "use:platform.system",
    ]

    # The module map reads the same position and goes the same way.
    assert offences('import sys\nm = (mm := sys.modules)["platform"]\nm.system()\n') == []
    assert offences('import sys\nmm = sys.modules\nm = mm["platform"]\nm.system()\n') == [
        "acquire:sys.modules['platform']",
        "use:platform.system",
    ]

    # So does the reference rule, on the object an attribute hangs off. There
    # the `import` is the acquisition either way, so read the *use* half.
    def uses(source: str) -> list[str]:
        return [o.what for o in _platform_offences(ast.parse(source)) if o.kind == "use"]

    assert uses("import platform\n(p := platform).system()\n") == []
    assert uses("import platform\np = platform\np.system()\n") == ["platform.system"]


def test_the_guard_admits_the_cross_file_handoff():
    """The residue's cross-file half, asserted rather than described.

    Round 7's docstring claimed a handoff could not launder an acquisition,
    because "whichever file acquires the module is flagged there". A later review
    showed that is false, and this is the counterexample it gave -- pinned here
    so the docstring and the scan cannot drift apart about it again.

    Both halves are scanned files and neither is flagged. It is a known and
    accepted boundary: closing it needs cross-file export analysis, which is out
    of proportion for a test-suite guard. If a later change starts catching one
    of these, this test fails and the residue bullet gets narrowed rather than
    the two quietly disagreeing.
    """

    def offences(source: str) -> list[str]:
        return [f"{o.kind}:{o.what}" for o in _platform_offences(ast.parse(source))]

    # The importer becomes a plain value in A, which never calls it; B calls it
    # without importing anything that would tell the scan what it is.
    helper_a = "import importlib\nload = importlib.import_module\n"
    helper_b = 'from tests.helper_a import load\nmodule = load("platform")\nmodule.system()\n'
    assert offences(helper_a) == []
    assert offences(helper_b) == []

    # ...and it passes for that reason, not because the forms are unrecognisable:
    # written in one file, the very same lines are both an acquisition and a use.
    assert offences(helper_a + helper_b.partition("\n")[2]) == [
        "acquire:load('platform')",
        "use:platform.system",
    ]

    # The same hole would run through an exemption: an exempt module holds the
    # real module object, and re-exporting it binds `platform` in a file whose
    # own syntax says only that some name came from some test module. Today
    # there is none, so no test module may hold it to re-export.
    assert PLATFORM_IMPORT_EXEMPT == {}
    assert offences('from tests.some_helper import platform\nplatform.system()\n') == []
    # Which is not a claim that `platform` is a word the scan ignores -- the
    # import that really names the module is still an acquisition.
    assert offences('import platform\nplatform.system()\n') == [
        "acquire:import platform",
        "use:platform.system",
    ]
