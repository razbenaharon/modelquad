# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad doctor``: can a review run here, and if not, what to do next.

Five checks, each ``ok`` (True, False, or None for unknown) with one line and
one next step. The Codex check runs Codex's own ``--version`` and ``features
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


def run(project: str | None = None) -> dict:
    checks = [_platform(), _codex(), _connection(), _settings(), _folder(project)]
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
