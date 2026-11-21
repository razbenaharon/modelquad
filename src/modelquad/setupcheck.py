# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad doctor``: can a review run here, and if not, what to do next.

Six checks, each ``ok`` (True, False, or None for unknown) with one line and
one next step. The last reads Claude Code's settings files for a permission
rule that names a ModelQuad tool by its name before 0.5.0; it never writes
them and shows only the rules that name ModelQuad. The Codex check runs Codex's own ``--version`` and ``features
list``, which contact no provider; nothing here starts a review or sends a
file anywhere.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _check(name: str, ok: bool | None, detail: str, next_step: str | None = None) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "next": next_step}


def _platform() -> dict:
    from .hostruntime import unsupported_platform_message

    message = unsupported_platform_message()
    if message is None:
        return _check("platform", True, "Native Windows.")
    return _check("platform", False, message)


def _codex() -> dict:
    """Whether the installed Codex CLI passes the same admission a review needs."""
    from .execution import noshell
    from .execution.codex import CodexExecutor, _probe, filter_windows_apps_alias_from_path, worker_environment
    from .execution.codex_worker_mcp import run_inventory

    executor = CodexExecutor()
    reason = executor.unavailable_reason()
    if reason == "cli_not_found":
        return _check("codex", False, "The Codex CLI was not found.",
                      "Install it (https://developers.openai.com/codex/cli) and sign in with: codex login")
    if reason is not None:
        return _check("codex", False, noshell.refusal_message(reason),
                      "Install the native Codex CLI and try again.")
    if os.name != "nt":
        return _check("codex", False, noshell.refusal_message("unsupported_platform"))
    try:
        resolved = executor._resolve()
        version = _probe(resolved)
        env = worker_environment()
        if "PATH" in env:
            env["PATH"] = filter_windows_apps_alias_from_path(env["PATH"])
        with tempfile.TemporaryDirectory(prefix="modelquad-doctor-", ignore_cleanup_errors=True) as empty:
            qualification = noshell.qualify(resolved, version, cwd=empty, env=env, runner=run_inventory)
    except Exception:  # noqa: BLE001 -- unknown, with no path or error text
        return _check("codex", None, "The Codex CLI could not be checked.", "Run: codex --version")
    if qualification.qualified:
        return _check("codex", True, f"Codex CLI {version}: its listing confirms the tools "
                                     "ModelQuad switches off are off.")
    return _check("codex", False,
                  noshell.refusal_message(qualification.refusal or "codex_feature_unverified"),
                  "Update the Codex CLI, then run this check again.")


def _connection() -> dict:
    from . import settings_view

    installed = settings_view._claude_installed()
    if installed is True:
        return _check("claude_code", True, "ModelQuad is connected to Claude Code.")
    if installed is False:
        return _check("claude_code", False, "ModelQuad is not connected to Claude Code.",
                      "Install the plugin, or run: modelquad install claude")
    return _check("claude_code", None, "Whether ModelQuad is connected to Claude Code could not be read.")


def _settings() -> dict:
    from . import grant, review_ledger, switch

    state = switch.enabled()
    if state is None:
        return _check("settings", False, switch.UNKNOWN_MESSAGE.split(", so")[0] + ".",
                      "Fix or remove preferences.json in ModelQuad's state folder.")
    try:
        if review_ledger.ledger_path().is_file():
            review_ledger.connect().close()
    except Exception:  # noqa: BLE001
        return _check("settings", False, "The review ledger could not be opened.",
                      "Move review-ledger.db out of ModelQuad's state folder; a new one is created.")
    mode = "automatic in every folder" if grant.granted(None) else "Claude Code asks before each call"
    return _check("settings", True, f"ModelQuad is {'on' if state else 'off'}; {mode}.",
                  None if state else "Turn it on with: modelquad on")


def _folder(project: str | None) -> dict:
    from . import localpath, repo_policy, repo_settings

    folder = os.path.abspath(project or os.getcwd())
    if localpath.reason(folder) is not None:
        return _check("folder", False, repo_policy.UNUSABLE_MESSAGE)
    if not Path(folder).is_dir():
        return _check("folder", False, "That project folder does not exist.")
    try:
        if repo_settings.read_mode(folder) == "off":
            return _check("folder", True, "You turned Codex off for this repository.",
                          "To allow it again: modelquad settings repo inherit")
        policy = repo_policy.load(folder)
    except Exception:  # noqa: BLE001
        return _check("folder", None, "This folder's rules could not be read.")
    if policy.refusal is not None:
        return _check("folder", False, repo_policy.message(policy.refusal, policy))
    facts = policy.facts()
    if facts["source"] == repo_policy.BUILTIN.source:
        return _check("folder", True, "No .modelquad.toml here: the built-in limits apply "
                                      f"({facts['max_files']} files, {facts['max_total_kib']} KiB).")
    return _check("folder", True, "This repository's .modelquad.toml allows: "
                  + ", ".join(facts["task_kinds"]) + f"; {facts['max_files']} files, "
                  f"{facts['max_total_kib']} KiB.",
                  "Its account rule cannot be met: this build does not read which Codex account "
                  "is signed in, so every call here is refused." if facts["account"] == "workspace" else None)


#: Claude Code settings files whose permission rules can name ModelQuad's tools:
#: (label, folder kind, path inside it). Read only; never written.
PERMISSION_FILES = (
    ("your Claude Code settings", "user", "settings.json"),
    ("your local Claude Code settings", "user", "settings.local.json"),
    ("this project's .claude/settings.json", "project", ".claude/settings.json"),
    ("this project's .claude/settings.local.json", "project", ".claude/settings.local.json"),
)


def renamed_rule(rule: str) -> str | None:
    """The rule to use since 0.5.0 for a permission rule that names a ModelQuad
    tool by its name before 0.5.0; None when the rule names no old name.

    Before 0.5.0 the plugin's server was named ``modelquad`` (tools
    ``mcp__plugin_modelquad_modelquad__modelquad_<tool>``) and every tool
    carried a ``modelquad_`` prefix (``mcp__modelquad__modelquad_<tool>``
    without the plugin). ``mcp__modelquad`` and ``mcp__modelquad__*`` still
    match: the install's server kept its name.
    """
    from .branding import INSTALL_TOOL_PREFIX, OLD_PLUGIN_SERVER_ID, OLD_TOOL_NAMES, PLUGIN_TOOL_PREFIX

    if not isinstance(rule, str):
        return None
    text = rule.strip()
    renamed = {old: new for new, old in OLD_TOOL_NAMES.items()}
    if text == OLD_PLUGIN_SERVER_ID:
        return PLUGIN_TOOL_PREFIX.rstrip("_")
    if text.startswith(OLD_PLUGIN_SERVER_ID + "__"):
        tool = text[len(OLD_PLUGIN_SERVER_ID) + 2:]
        return PLUGIN_TOOL_PREFIX + renamed.get(tool, tool)
    if text.startswith(INSTALL_TOOL_PREFIX):
        tool = text[len(INSTALL_TOOL_PREFIX):]
        if tool in renamed:
            return INSTALL_TOOL_PREFIX + renamed[tool]
    return None


def _covered(new_rule: str, rules: set[str]) -> bool:
    """Whether one of ``rules`` already matches what ``new_rule`` matches."""
    server = new_rule.split("__")[:2]
    server_id = "__".join(server)
    return bool({new_rule, server_id, server_id + "__*"} & rules)


def _read_permissions(path: Path) -> dict | None:
    """``permissions.allow`` and ``permissions.deny`` of one settings file: {} when the
    file is absent, None when it cannot be read. Nothing else in the file is kept."""
    import json

    try:
        if not path.is_file():
            return {} if not path.exists() else None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None
    permissions = payload.get("permissions") if isinstance(payload, dict) else None
    if not isinstance(permissions, dict):
        return {}
    found = {}
    for kind in ("allow", "deny"):
        value = permissions.get(kind)
        if isinstance(value, list):
            found[kind] = [item for item in value if isinstance(item, str)]
    return found


def _permission_rules(project: str | None, claude_home: Path | None = None) -> dict:
    """Whether a Claude Code permission rule still names a ModelQuad tool by its name
    before 0.5.0. A stale deny no longer protects; a stale allow only brings the
    prompt back. Reads the settings files; never writes them."""
    if claude_home is None:
        try:
            from .adapters.paths import default_target_dir

            claude_home = default_target_dir("claude")
        except Exception:  # noqa: BLE001 -- unknown, not a crash
            claude_home = None
    folder = Path(os.path.abspath(project or os.getcwd()))
    read: dict[str, dict] = {}
    unreadable = []
    for label, kind, relative in PERMISSION_FILES:
        base = claude_home if kind == "user" else folder
        if base is None:
            unreadable.append(label)
            continue
        found = _read_permissions(Path(base) / relative)
        if found is None:
            unreadable.append(label)
        else:
            read[label] = found
    denied = {rule.strip() for found in read.values() for rule in found.get("deny", [])}
    open_denies, covered_denies, allows = [], [], []
    for label, found in read.items():
        for kind in ("deny", "allow"):
            for rule in found.get(kind, []):
                new = renamed_rule(rule)
                if new is None:
                    continue
                item = {"file": label, "kind": kind, "rule": rule.strip(), "use": new}
                if kind == "allow":
                    allows.append(item)
                elif _covered(new, denied):
                    covered_denies.append(item)
                else:
                    open_denies.append(item)
    stale = open_denies + allows + covered_denies

    def line(item: dict) -> str:
        return f"{item['kind']} rule {item['rule']} in {item['file']}: use {item['use']}"

    not_read = (" " + ", ".join(unreadable) + " could not be read.") if unreadable else ""
    if open_denies:
        check = _check("permission_rules", False,
                       "A deny rule names a ModelQuad tool by its name before 0.5.0 and no longer "
                       "keeps that tool out: " + "; ".join(line(item) for item in open_denies) + "."
                       + not_read,
                       "Add the new name to the same deny list; keep the old one if an older "
                       "ModelQuad may still run there. ModelQuad does not change these files.")
    elif unreadable:
        check = _check("permission_rules", None,
                       "Whether a Claude Code deny rule still names an old ModelQuad tool name is "
                       "unknown:" + not_read
                       + "".join(f" An allow {line(item)[len('allow '):]}." for item in allows))
    elif allows:
        check = _check("permission_rules", True,
                       "An allow rule names a ModelQuad tool by its name before 0.5.0, so Claude "
                       "Code asks again before that tool: " + "; ".join(line(item) for item in allows) + ".",
                       "To skip that prompt again, put the new name in the allow rule. ModelQuad "
                       "does not change these files.")
    else:
        check = _check("permission_rules", True,
                       "No Claude Code permission rule needs a new ModelQuad tool name"
                       + (" (a deny rule with an old name is also there under the new name)."
                          if covered_denies else "."))
    check["stale_rules"] = stale
    check["unreadable"] = unreadable
    return check


def run(project: str | None = None) -> dict:
    checks = [_platform(), _codex(), _connection(), _settings(), _folder(project),
              _permission_rules(project)]
    return {"ok": all(item["ok"] is not False for item in checks), "checks": checks}


def lines(report: dict) -> list[str]:
    marks = {True: "ok", False: "PROBLEM", None: "unknown"}
    out = []
    for item in report["checks"]:
        out.append(f"[{marks[item['ok']]}] {item['detail']}")
        if item["next"]:
            out.append(f"       {item['next']}")
    out.append("")
    out.append("Everything a review needs is in place." if report["ok"] and all(
        item["ok"] is True for item in report["checks"])
        else "A review can run." if report["ok"] else "A review cannot run until the problems above are fixed.")
    return out
