# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which named files ModelQuad may attach to a Codex consultation.

ModelQuad reads the files it sends itself, outside Claude Code's own file
tools. So before reading anything it applies the user's existing read
restrictions: Claude Code ``Read(...)`` deny rules from every settings scope,
files Git ignores, and ``.modelquadignore``. A file any of them excludes is
skipped with a closed reason, never read. Settings that exist but cannot be
read or parsed skip every attachment: ModelQuad cannot honour rules it cannot
read.

The working folder is whatever Claude names, which may be a subfolder of the
project or a folder above it. So project settings and ``.modelquadignore``
are read from the working folder, every folder between it and each named
file, and every folder above them, and each rule is applied relative to each
of those folders. Rules from a folder that is not the project only widen
what is skipped.

Nothing here reads a candidate file's contents. Git is asked only which of the
named paths it ignores (``git check-ignore``); without Git, or outside a
repository, that one check is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess

DENIED = "denied_by_claude_settings"
SETTINGS_UNREADABLE = "claude_settings_unreadable"
GIT_IGNORED = "git_ignored"
MODELQUAD_IGNORED = "modelquad_ignored"
IGNORE_FILE_UNREADABLE = "modelquadignore_unreadable"
#: Every reason this module skips a file for. None of them refuses a run.
POLICY_REASONS = frozenset({DENIED, SETTINGS_UNREADABLE, GIT_IGNORED, MODELQUAD_IGNORED,
                            IGNORE_FILE_UNREADABLE})
IGNORE_FILE = ".modelquadignore"
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_IGNORE_BYTES = 256 * 1024
GIT_SECONDS = 10.0

#: Claude Code's managed (organisation) settings file on this platform.
MANAGED_SETTINGS = (Path(r"C:\Program Files\ClaudeCode\managed-settings.json") if os.name == "nt"
                    else Path("/etc/claude-code/managed-settings.json"))
_FLAGS = re.IGNORECASE if os.name == "nt" else 0


def _user_settings_directory() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def settings_files(root, folders=None) -> tuple[Path, ...]:
    """Managed, user, then project and local settings of each folder, in scope order."""
    folders = (Path(root),) if folders is None else tuple(folders)
    paths = [MANAGED_SETTINGS, _user_settings_directory() / "settings.json"]
    for folder in folders:
        paths += [folder / ".claude" / "settings.json", folder / ".claude" / "settings.local.json"]
    return tuple(dict.fromkeys(paths))


def _upward(folder: Path, home: Path) -> tuple[Path, ...]:
    """``folder`` and the folders above it, up to but not including the home folder.

    The home folder's ``.claude`` is the default user settings folder, read
    as user settings (or not at all when ``CLAUDE_CONFIG_DIR`` moves them),
    and above it there is no project. A folder outside the home folder is
    followed up to its drive's root.
    """
    chain = [folder]
    for current in folder.parents:
        if current == home:
            break
        chain.append(current)
    return tuple(chain)


def folders_above(root, names) -> tuple[Path, ...]:
    """The working folder, each folder holding a named file, and every folder above them."""
    root, home = Path(os.path.abspath(root)), Path(os.path.abspath(Path.home()))
    found = dict.fromkeys(_upward(root, home))
    for name in names:
        for folder in _upward(root.joinpath(*name.split("/")).parent, home):
            if folder in found:
                break  # everything above a known folder is known already
            found[folder] = None
    return tuple(found)


def translate(glob: str) -> str:
    """A regular expression for one gitignore-style glob over ``/`` paths.

    ``**`` as a whole segment spans any number of folders (zero included),
    ``*`` and ``?`` stay inside one segment, ``[...]`` is a character class
    and a backslash escapes the next character.
    """
    out, index, size = [], 0, len(glob)
    while index < size:
        char = glob[index]
        if char == "*":
            end = index + 1
            while end < size and glob[end] == "*":
                end += 1
            whole = end - index >= 2 and (index == 0 or glob[index - 1] == "/")
            if whole and end == size:
                out.append(".*")
            elif whole and glob[end] == "/":
                out.append("(?:[^/]*/)*")
                end += 1
            else:
                out.append("[^/]*")
            index = end
            continue
        if char == "?":
            out.append("[^/]")
        elif char == "[":
            close = glob.find("]", index + 2)
            if close == -1:
                out.append(re.escape(char))
            else:
                body = glob[index + 1:close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("(?!/)[" + body.replace("\\", "\\\\") + "]")
                index = close + 1
                continue
        elif char == "\\" and index + 1 < size:
            out.append(re.escape(glob[index + 1]))
            index += 2
            continue
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern
    negate: bool = False
    directory_only: bool = False


def parse_ignore(text: str) -> tuple[_Rule, ...]:
    """Rules from gitignore-syntax text: comments, ``!`` negation, ``dir/``."""
    rules = []
    for line in text.splitlines():
        line = re.sub(r"(?<!\\)[ \t]+$", "", line)
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        elif line.startswith(("\\!", "\\#")):
            line = line[1:]
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        body = translate(line.lstrip("/"))
        try:
            pattern = re.compile(body if anchored else "(?:.*/)?" + body, _FLAGS)
        except re.error:
            continue
        rules.append(_Rule(pattern, negate, directory_only))
    return tuple(rules)


def ignored(rules, name: str) -> bool:
    """Gitignore semantics: an excluded folder excludes everything inside it."""
    parts = name.split("/")
    for index in range(1, len(parts) + 1):
        candidate, folder = "/".join(parts[:index]), index < len(parts)
        verdict = False
        for rule in rules:
            if rule.directory_only and not folder:
                continue
            if rule.pattern.fullmatch(candidate):
                verdict = not rule.negate
        if verdict:
            return True
    return False


def posix_absolute(path) -> str:
    """``C:\\a\\b`` as ``/c/a/b``: the form Claude Code matches Read rules in."""
    text = os.path.abspath(os.fspath(path)).replace("\\", "/")
    match = re.fullmatch(r"([A-Za-z]):(/.*)?", text)
    if match:
        text = "/" + match[1].lower() + (match[2] or "")
    return text.rstrip("/") or "/"


def _read_rule(rule: str, root: Path, home: Path) -> re.Pattern | None:
    """The absolute-path pattern one ``Read(...)`` deny rule covers, else None.

    The documented forms: ``//absolute``, ``~/home``, ``/project-root`` and a
    relative path (``./`` optional). A bare ``Read`` denies everything. A
    relative pattern without a folder part matches at any depth, as
    gitignore does; a trailing ``/`` also covers the folder's contents.
    Anything ambiguous is read the wider way: over-skipping is the safe error.
    """
    match = re.fullmatch(r"\s*Read\s*(?:\((.*)\))?\s*", rule, re.DOTALL)
    if match is None:
        return None
    spec = (match[1] or "").strip()
    if spec in {"", "*", "**", "/**", "./**"}:
        return re.compile(".*", re.DOTALL)
    base, lead = "", ""
    if re.match(r"[A-Za-z]:[\\/]", spec):
        spec = "//" + spec[0].lower() + spec[2:].replace("\\", "/")
    elif spec.startswith("\\\\"):
        spec = spec.replace("\\", "/")
    if spec.startswith("//"):
        glob = spec[1:]
        drive = re.match(r"/([A-Za-z]):(/|$)", glob)
        if drive:
            glob = "/" + drive[1].lower() + glob[3:]
        if os.name == "nt":
            glob = re.sub(r"^/([A-Za-z])(?=/|$)", lambda found: "/" + found[1].lower(), glob)
        if not re.match(r"/[A-Za-z](/|$)", glob):
            # Not a drive: perhaps a network share, whose paths keep both
            # leading slashes (``//server/share``). Match either form.
            lead = "/?"
    elif spec == "~" or spec.startswith("~/"):
        base, glob = posix_absolute(home), spec[2:] or "**"
    elif spec.startswith("/"):
        base, glob = posix_absolute(root), spec[1:]
    else:
        relative = spec[2:] if spec.startswith("./") else spec
        base = posix_absolute(root)
        glob = relative if "/" in relative.rstrip("/") else "**/" + relative
    glob = glob.rstrip("/") or "**"
    prefix = re.escape(base.rstrip("/")) + "/" if base else ""
    try:
        return re.compile(lead + prefix + translate(glob.lstrip("/") if base else glob), _FLAGS | re.DOTALL)
    except re.error:
        return re.compile(".*", re.DOTALL)


def _deny_rules(path: Path) -> list[str] | None:
    """Deny rules from one settings file; [] when absent, None when unreadable."""
    try:
        if path.stat().st_size > MAX_SETTINGS_BYTES:
            return None
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    if type(data) is not dict:
        return None
    permissions = data.get("permissions")
    if permissions is None:
        return []
    if type(permissions) is not dict:
        return None
    deny = permissions.get("deny")
    if deny is None:
        return []
    if type(deny) is not list or any(type(rule) is not str for rule in deny):
        return None
    return deny


def claude_read_denials(root, folders=None) -> tuple[re.Pattern, ...] | None:
    """Every Read deny pattern in scope; None if any settings file is unreadable.

    Each rule is read relative to every one of ``folders`` (default: just
    ``root``), since which of them is the project Claude Code was opened in
    is not known here.
    """
    root = Path(os.path.abspath(root))
    folders = (root,) if folders is None else tuple(folders)
    home = Path.home()
    patterns = {}
    for path in settings_files(root, folders):
        rules = _deny_rules(path)
        if rules is None:
            return None
        for rule in rules:
            for base in folders:
                pattern = _read_rule(rule, base, home)
                if pattern is not None:
                    patterns.setdefault(pattern.pattern, pattern)
    return tuple(patterns.values())


def _denied(patterns, root: Path, name: str) -> bool:
    absolute = posix_absolute(root.joinpath(*name.split("/")))
    candidates = [absolute]
    while "/" in absolute.strip("/"):
        absolute = absolute.rsplit("/", 1)[0]
        candidates.append(absolute)
    return any(pattern.fullmatch(candidate) for pattern in patterns for candidate in candidates)


def modelquad_ignore_rules(root) -> tuple[_Rule, ...] | None:
    """Rules from ``<root>/.modelquadignore``; None when it exists but cannot be read."""
    path = Path(root) / IGNORE_FILE
    try:
        if path.stat().st_size > MAX_IGNORE_BYTES:
            return None
        return parse_ignore(path.read_bytes().decode("utf-8-sig"))
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except (OSError, UnicodeError):
        return None


def git_ignored(root, names) -> frozenset[str]:
    """The subset of ``names`` Git ignores in ``root``; empty when Git cannot say.

    Git runs with inherited ``GIT_*`` overrides removed, file-system monitors
    and hooks off, and an executable planted in the folder never chosen.
    ``check-ignore`` reads ignore rules only; a tracked file is not ignored.
    """
    names = list(names)
    if not names:
        return frozenset()
    from ..executable_resolution import resolve_executable
    from .. import gitmeta as changes
    from ..procutil import no_window_kwargs
    try:
        executable = resolve_executable("git", exclude_roots=changes._git_exclude_roots(Path(root)))
    except Exception:  # noqa: BLE001 - no safe Git means no Git check
        return frozenset()
    if executable.path is None:
        return frozenset()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    command = [executable.path, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
               "check-ignore", "--stdin", "-z"]
    try:
        completed = subprocess.run(
            command, cwd=os.fspath(root), env=env, input=("\0".join(names) + "\0").encode("utf-8"),
            capture_output=True, timeout=GIT_SECONDS, check=False,
            **no_window_kwargs(detach_stdin=False))
    except (OSError, subprocess.SubprocessError, ValueError):
        return frozenset()
    # 0: some ignored, 1: none ignored; anything else (128: not a repository).
    if completed.returncode not in (0, 1):
        return frozenset()
    reported = {item.decode("utf-8", "replace") for item in completed.stdout.split(b"\0") if item}
    if os.name == "nt":
        reported = {item.casefold() for item in reported}
        return frozenset(name for name in names if name.casefold() in reported)
    return frozenset(name for name in names if name in reported)


def _ignored_above(ignores, root: Path, name: str) -> bool:
    """Whether a ``.modelquadignore`` in a folder holding ``name`` covers it."""
    target = root.joinpath(*name.split("/"))
    for folder, rules in ignores:
        try:
            relative = target.relative_to(folder)
        except ValueError:
            continue
        if ignored(rules, relative.as_posix()):
            return True
    return False


def screen(root, names) -> tuple[list[str], list[tuple[str, str]]]:
    """Split validated relative names into (allowed, skipped with a reason).

    Order: unreadable Claude settings skip everything; then each name is
    checked against Read deny rules, ``.modelquadignore`` and Git.
    """
    root = Path(os.path.abspath(root))
    names = list(dict.fromkeys(names))
    folders = folders_above(root, names)
    denials = claude_read_denials(root, folders)
    if denials is None:
        return [], [(name, SETTINGS_UNREADABLE) for name in names]
    ignores = []
    for folder in folders:
        rules = modelquad_ignore_rules(folder)
        if rules is None:
            return [], [(name, IGNORE_FILE_UNREADABLE) for name in names]
        if rules:
            ignores.append((folder, rules))
    allowed, skipped = [], []
    for name in names:
        if _denied(denials, root, name):
            skipped.append((name, DENIED))
        elif _ignored_above(ignores, root, name):
            skipped.append((name, MODELQUAD_IGNORED))
        else:
            allowed.append(name)
    hidden = git_ignored(root, allowed)
    skipped += [(name, GIT_IGNORED) for name in allowed if name in hidden]
    return [name for name in allowed if name not in hidden], skipped
