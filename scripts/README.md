# Scripts

Development tooling for this repository. None of it ships: the product is
`src/modelquad/`.

## `check.py`: pre-flight validation

Stdlib-only and identical on PowerShell and POSIX shells, so it runs from a
fresh clone with nothing installed.

    python scripts/check.py --list
    python scripts/check.py fast        # docs + secrets, seconds
    python scripts/check.py docs        # links resolve, AGENTS.md matches the code, commands run on Windows, no banned phrases
    python scripts/check.py secrets     # credential shapes in authored source
    python scripts/check.py review      # fast + the tests the diff touches
    python scripts/check.py focused     # compile, import, and the tests the diff touches
    python scripts/check.py python      # the whole suite, import root pinned here
    python scripts/check.py all

It sets `PYTHONPATH` itself and prints the `modelquad.__file__` it resolved,
which removes two traps: a POSIX-only command on a Windows-first project, and a
worktree suite importing another checkout's source. It echoes every command,
so you can run them by hand instead.

Its own tests: `tests/test_development_policy.py` and `tests/test_devenv.py`.

## The rest

- `devenv.py`: where development and test child processes put their temporary
  files (`MODELQUAD_TEMP_ROOT`); used by `check.py` and the root `conftest.py`.
- `prune_temp.py`: removes known test leftovers, or explicitly selected
  per-run temporary roots. Dry-run by default.
- `windowwatch.py`: records console windows opened while something runs; the
  manual observation tool behind the no-window tests. It has no test of its
  own: run it by hand when a console window needs to be measured again.
- `sync_plugin.py`: copies the packaged Claude skills and MCP entry into
  `plugin/`; `tests/test_plugin_prototype.py` fails while a copy differs.
- `third_party_inventory.py`: generates the third-party dependency and licence
  inventory in `THIRD_PARTY.md`.
