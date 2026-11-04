# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The installed ``modelquad`` entry runs the hooks exactly as ``cli.main`` does.

``cli_entry.main`` answers ``usage-hook`` and ``hook-gate`` on a light path
that never imports the full CLI. Each test runs both entries in a fresh
process with a fake HOME/USERPROFILE/LOCALAPPDATA -- the usage directory and
the state root are resolved once at import, so only a fresh process sees the
fake home -- and compares the files and output they produce.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import modelquad
import pytest

from conftest import no_window_kwargs

_RUN = """
import json, sys
from pathlib import Path
mode, argv, out = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
if mode == 'pause':
    from modelquad.service import db, history
    history.put_routing_paused(db.DEFAULT_DB_PATH, True)
    code = 0
else:
    if mode == 'entry':
        from modelquad.cli_entry import main
    else:
        from modelquad.cli import main
    code = main(argv)
from modelquad import paths
from modelquad.providers.base import DEFAULT_USAGE_DIRECTORY
out.write_text(json.dumps({
    'code': code, 'state_root': str(paths.state_root()),
    'usage_directory': str(DEFAULT_USAGE_DIRECTORY),
    'modules': sorted(name for name in sys.modules
                      if name.startswith('modelquad') or name == 'yaml'),
}), encoding='utf-8')
"""

# Never imported by the light entry: the whole point of the fast path.
_HEAVY = ("modelquad.cli", "modelquad.router", "modelquad.registry", "yaml",
          "modelquad.providers.factory", "modelquad.service.history")


def _run(home: Path, mode: str, argv: list[str], stdin: bytes = b"{}") -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in {"MODELQUAD_STATE_ROOT", "MODELQUAD_STATE_MODE", "CODEX_HOME"}}
    env.update(HOME=str(home), USERPROFILE=str(home),
               APPDATA=str(home / "AppData" / "Roaming"),
               LOCALAPPDATA=str(home / "AppData" / "Local"),
               PYTHONPATH=str(Path(modelquad.__file__).resolve().parent.parent))
    out = home / "result.json"
    home.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-c", _RUN, mode, json.dumps(argv), str(out)],
        input=stdin, env=env, capture_output=True, timeout=60, **no_window_kwargs())
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    result = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    result["stdout"] = completed.stdout.decode("utf-8")
    return result


def _files(home: Path) -> set[str]:
    return {path.relative_to(home).as_posix() for path in home.rglob("*")
            if path.is_file() and not path.name.endswith(".lock")}


def _status_payload(now: int | None = None) -> bytes:
    now = int(time.time()) if now is None else now
    return json.dumps({
        "session_id": "session-one",
        "version": "2.1.280",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 85, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 40, "resets_at": now + 300000},
        },
    }).encode("utf-8")


def _seed_codex(home: Path) -> None:
    usage = home / ".claude"
    usage.mkdir(parents=True)
    (usage / "usage-codex.json").write_text(json.dumps({
        "provider": "codex", "source": "codex_app_server",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "five_hour_used_percentage": 30, "five_hour_remaining_percentage": 70,
    }), encoding="utf-8")


def _resets_clock(now: int) -> str:
    from modelquad.assistance import local_clock

    return local_clock(datetime.fromtimestamp(now + 3600, timezone.utc),
                       datetime.fromtimestamp(now, timezone.utc))


def test_installed_status_line_writes_and_prints_what_the_full_cli_does(tmp_path):
    results, files = {}, {}
    now = int(time.time())
    for mode in ("entry", "cli"):
        home = tmp_path / mode
        _seed_codex(home)
        results[mode] = _run(home, mode, ["usage-hook", "claude"], _status_payload(now))
        files[mode] = _files(home)
        assert results[mode]["code"] == 0

    entry, home = results["entry"], tmp_path / "entry"
    # The real usage directory (~/.claude), separate from ModelQuad state.
    assert Path(entry["usage_directory"]) == home / ".claude"
    assert (home / ".claude" / "usage-claude.json").is_file()
    # A development checkout keeps state in ~/.modelquad, an installed wheel in
    # ~/.local/share/modelquad (CI runs the installed one); the light entry
    # must pick the same one the full CLI does.
    state_root = Path(entry["state_root"])
    assert state_root in {home / ".modelquad", home / ".local" / "share" / "modelquad"}
    assert state_root.relative_to(home) == Path(results["cli"]["state_root"]).relative_to(tmp_path / "cli")
    # The host observation the MCP server reads.
    assert (Path(entry["state_root"]) / "host-observation-claude.json").is_file()
    assert files["entry"] == files["cli"]
    # Both providers, "used" labels, and the near-limit note -- the same line.
    assert entry["stdout"] == results["cli"]["stdout"]
    assert entry["stdout"].strip() == (
        f"Claude 5h 15% left, resets {_resets_clock(now)} / 7d 60% left"
        " | Codex 5h 70% left / 7d unknown"
        f" | Claude 5h near its limit, resets {_resets_clock(now)}: you can wait, or ask Codex for a review")
    # The import budget: the status line never pays for the full CLI.
    assert not set(_HEAVY) & set(entry["modules"])


def test_the_segment_runs_on_the_installed_path_and_matches_the_full_cli(tmp_path):
    """``--segment`` is the compact part for a status line the user already has."""
    results, files = {}, {}
    now = int(time.time())
    for mode in ("entry", "cli"):
        home = tmp_path / mode
        _seed_codex(home)
        results[mode] = _run(home, mode, ["usage-hook", "claude", "--segment"], _status_payload(now))
        files[mode] = _files(home)
        assert results[mode]["code"] == 0
    assert files["entry"] == files["cli"]
    assert results["entry"]["stdout"] == results["cli"]["stdout"]
    assert results["entry"]["stdout"].strip() == (
        "Claude 5h 15% left | Codex 5h 70% left"
        f" | Claude 5h near its limit, resets {_resets_clock(now)}: you can wait, or ask Codex for a review")
    assert results["entry"]["stdout"].strip().isascii()
    assert not set(_HEAVY) & set(results["entry"]["modules"])
    written = json.loads((tmp_path / "entry" / ".claude" / "usage-claude.json").read_text(encoding="utf-8"))
    assert written["five_hour_used_percentage"] == 85


@pytest.mark.parametrize("mode", ["entry", "cli"])
@pytest.mark.parametrize("argv", [["usage-hook", "claude", "--quiet"],
                                  ["usage-hook", "--quiet", "claude"],
                                  ["usage-hook", "claude", "--segment", "--quiet"]])
def test_quiet_records_the_same_files_and_prints_nothing(tmp_path, mode, argv):
    home = tmp_path / "home"
    _seed_codex(home)
    result = _run(home, mode, argv, _status_payload())
    assert result["code"] == 0
    assert result["stdout"] == ""
    written = json.loads((home / ".claude" / "usage-claude.json").read_text(encoding="utf-8"))
    assert written["five_hour_used_percentage"] == 85
    assert (Path(result["state_root"]) / "host-observation-claude.json").is_file()


def test_an_explicit_usage_directory_is_honoured_by_the_installed_entry(tmp_path):
    home, chosen = tmp_path / "home", tmp_path / "chosen"
    result = _run(home, "entry", ["--usage-directory", str(chosen), "usage-hook", "claude"],
                  _status_payload())
    assert result["stdout"].startswith("Claude 5h 15% left, resets ")
    assert "/ 7d 60% left | Codex 5h unknown" in result["stdout"]
    assert (chosen / "usage-claude.json").is_file()
    assert not (home / ".claude" / "usage-claude.json").exists()


@pytest.mark.parametrize("stdin", [b"not json", b"[1, 2]", b""])
def test_installed_gate_always_exits_zero(tmp_path, stdin):
    result = _run(tmp_path / "home", "entry", ["hook-gate"], stdin)
    assert result["code"] == 0
    assert result["stdout"] == ""
    assert "modelquad.cli" not in result["modules"]
