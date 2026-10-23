# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The per-installation secret behind every change through the local service.

`service/auth.py` creates `<state_root>/service-secret` exclusively, with an
access list naming only the current user and SYSTEM (Windows) or mode 0600
(POSIX), and reads it back through the same verification on every service
start. These pin: what the file is, what a tampered file does to the service,
that the value never reaches a response, a log or doctor, and how the clients
that open the window receive it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modelquad import cli as cli_module
from modelquad.procutil import no_window_kwargs
from modelquad.service import auth, trust
from modelquad.service.app import create_app
from modelquad.service.auth import ServiceSecret, ensure_service_secret, read_service_secret

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_the_development_flag_adds_no_cors_middleware():
    """The development-origin relaxation is removed entirely, not gated."""
    for flag in ("0", "1"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("MODELQUAD_DEV", flag)
            app = create_app(poll_providers=False, write_secret="synthetic-test-secret")
            assert all(m.cls.__name__ != "CORSMiddleware" for m in app.user_middleware)
            client = TestClient(app, base_url="http://127.0.0.1:8765")
            for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
                response = client.put(
                    "/api/routing-state",
                    json={"routing_paused": False},
                    headers={"Origin": origin, "Sec-Fetch-Site": "cross-site",
                             "X-ModelQuad-Token": "synthetic-test-secret"},
                )
                assert response.status_code == 403
                preflight = client.options(
                    "/api/routing-state",
                    headers={"Origin": origin, "Access-Control-Request-Method": "PUT"},
                )
                assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("flag", [None, "1"])
def test_inspector_routes_are_removed_even_in_developer_mode(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("MODELQUAD_DEV_MODE", raising=False)
    else:
        monkeypatch.setenv("MODELQUAD_DEV_MODE", flag)
    app = create_app(db_path=tmp_path / "state.db", poll_providers=False, write_secret="inspector-secret")
    assert not any(path == "/dev" or path.startswith("/api/dev")
                   for path in app.openapi()["paths"])
    with TestClient(app, base_url="http://127.0.0.1", headers={"X-ModelQuad-Token": "inspector-secret"}) as client:
        assert client.get("/dev").status_code == 404
        assert client.get("/api/dev/decisions").status_code == 404
        assert client.get("/api/health").json()["service_role"] == "control-plane"


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------


def test_the_secret_is_created_once_and_read_back(tmp_path):
    root = tmp_path / "state"
    first = ensure_service_secret(root)
    assert first.usable and first.problem is None
    assert len(first.value) >= 43
    assert (root / auth.SECRET_FILE_NAME).read_text(encoding="ascii") == first.value
    # Every later start reads the same one; it is never replaced.
    assert ensure_service_secret(root).value == first.value
    assert read_service_secret(root) == first.value


def test_the_value_never_appears_in_a_repr(tmp_path):
    secret = ensure_service_secret(tmp_path)
    assert secret.value not in repr(secret)
    assert secret.value not in str(secret)


def test_a_reader_never_creates_the_file(tmp_path):
    assert read_service_secret(tmp_path) is None
    assert not (tmp_path / auth.SECRET_FILE_NAME).exists()


def test_an_unusable_state_root_is_a_problem_not_a_crash(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    secret = ensure_service_secret(blocker)
    assert not secret.usable
    assert secret.problem


def test_a_malformed_secret_is_refused_not_replaced(tmp_path):
    path = tmp_path / auth.SECRET_FILE_NAME
    auth._create_exclusively(path, "short")  # the right permissions, the wrong content
    secret = ensure_service_secret(tmp_path)
    assert not secret.usable
    assert "malformed" in secret.problem
    assert str(path) in secret.problem
    assert path.read_bytes() == b"short"


@posix_only
def test_posix_mode_is_0600(tmp_path):
    ensure_service_secret(tmp_path)
    assert (tmp_path / auth.SECRET_FILE_NAME).stat().st_mode & 0o777 == 0o600


@posix_only
def test_posix_group_readable_secret_is_refused(tmp_path):
    path = tmp_path / auth.SECRET_FILE_NAME
    ensure_service_secret(tmp_path)
    path.chmod(0o640)
    secret = ensure_service_secret(tmp_path)
    assert not secret.usable
    assert "other accounts" in secret.problem


# --------------------------------------------------------------------------
# Windows: the security descriptor, read back independently
# --------------------------------------------------------------------------


def _whoami_sid() -> str:
    out = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, **no_window_kwargs()
    ).stdout
    return out.strip().split(",")[-1].strip().strip('"')


def _sddl(path: Path) -> str:
    """The file's owner and DACL as SDDL, by a code path auth.py does not use."""

    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = 0x1 | 0x4  # OWNER | DACL
    needed = wintypes.DWORD()
    adv.GetFileSecurityW(str(path), info, None, 0, ctypes.byref(needed))
    buffer = ctypes.create_string_buffer(needed.value)
    assert adv.GetFileSecurityW(str(path), info, buffer, needed, ctypes.byref(needed))
    text = wintypes.LPWSTR()
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p,
    ]
    assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, info, ctypes.byref(text), None)
    try:
        return text.value
    finally:
        k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _parse_sddl(sddl: str) -> tuple[str, str, list[str]]:
    owner_part, dacl_part = sddl.split("D:", 1)
    owner = owner_part.removeprefix("O:")
    flags, _, aces = dacl_part.partition("(")
    return owner, flags, ["(" + ace for ace in ("(" + aces).split("(") if ace]


def _sid_value(trustee: str) -> str:
    """An SDDL trustee -- an alias such as ``NS``/``SY``/``BA`` or a full SID -- as
    its canonical SID string, resolved by Windows rather than by a table here.

    SDDL abbreviates well-known accounts: a runner that is NETWORK SERVICE
    reads back as ``O:NS``, not ``O:S-1-5-20``. Comparing text would fail it."""

    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    sid = ctypes.c_void_p()
    assert adv.ConvertStringSidToSidW(trustee, ctypes.byref(sid)), trustee
    text = wintypes.LPWSTR()
    try:
        assert adv.ConvertSidToStringSidW(sid, ctypes.byref(text)), trustee
        value = text.value
        k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        return value
    finally:
        k32.LocalFree(sid)


@windows_only
def test_sddl_aliases_resolve_to_the_same_sid():
    assert _sid_value("NS") == _sid_value("S-1-5-20") == "S-1-5-20"
    assert _sid_value("LS") == "S-1-5-19"
    assert _sid_value("SY") == "S-1-5-18"
    assert _sid_value("BA") == "S-1-5-32-544"


def _described_from_sddl(sddl: str) -> auth.SecurityDescription:
    """A descriptor built by Windows from ``sddl``, read by auth.py's own reader."""

    import ctypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetSecurityDescriptorOwner.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    sd = auth._security_descriptor(sddl)
    owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    defaulted, present = ctypes.c_int(), ctypes.c_int()
    assert adv.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(defaulted))
    assert adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
    return auth._describe_and_free(owner, dacl, sd)  # frees sd


@windows_only
@pytest.mark.parametrize(
    "account, alias",
    [("S-1-5-20", "NS"), ("S-1-5-19", "LS"), ("S-1-5-18", "SY")],
)
def test_verdicts_compare_accounts_by_sid_not_by_sddl_text(account, alias):
    # A service running as an account SDDL abbreviates is judged exactly as
    # one spelled out in full, whichever form the descriptor or caller uses.
    cases = {
        "private": "O:{o}D:P(A;;FA;;;{o})(A;;FA;;;SY)",
        "extra": "O:{o}D:P(A;;FA;;;{o})(A;;FA;;;SY)(A;;FR;;;WD)",
        "inherited": "O:{o}D:(A;;FA;;;{o})(A;;FA;;;SY)",
        "foreign owner": "O:BAD:P(A;;FA;;;{o})(A;;FA;;;SY)",
    }

    def verdicts(spelling: str, caller: str) -> dict[str, tuple[str, bool]]:
        out = {}
        for name, template in cases.items():
            described = _described_from_sddl(template.format(o=spelling))
            try:
                auth.verify_descriptor(described, caller)
                file_verdict = "ok"
            except auth.SecretFileError as exc:
                file_verdict = str(exc)
            out[name] = (file_verdict, auth.directory_is_private(described, caller))
        return out

    by_alias = verdicts(alias, alias)
    assert by_alias == verdicts(account, account) == verdicts(alias, account) == verdicts(account, alias)
    assert by_alias["private"] == ("ok", True)
    assert by_alias["extra"][1] is False and "another account" in by_alias["extra"][0]
    assert by_alias["inherited"][1] is False and "inherits" in by_alias["inherited"][0]
    assert "owned by another" in by_alias["foreign owner"][0]


@windows_only
def test_windows_secret_grants_exactly_the_user_and_system(tmp_path):
    ensure_service_secret(tmp_path)
    user = _whoami_sid()
    assert auth.current_user_sid() == user
    owner, flags, aces = _parse_sddl(_sddl(tmp_path / auth.SECRET_FILE_NAME))
    assert _sid_value(owner) == user
    assert "P" in flags, "the DACL must be protected: nothing inherited from the folder"
    granted = set()
    for ace in aces:
        kind, ace_flags, _rights, _obj, _inherit_obj, sid = ace.strip("()").split(";")
        assert kind == "A", ace
        assert "ID" not in ace_flags, f"inherited entry: {ace}"
        granted.add(_sid_value(sid))
    assert granted == {user, "S-1-5-18"}


@windows_only
def test_windows_secret_with_an_extra_entry_refuses_every_change(tmp_path):
    first = ensure_service_secret(tmp_path)
    path = tmp_path / auth.SECRET_FILE_NAME
    # Everyone: read -- the shape of a file another account could read.
    subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], capture_output=True, check=True, **no_window_kwargs())

    secret = ensure_service_secret(tmp_path)
    assert not secret.usable
    assert "another account" in secret.problem
    assert first.value not in secret.problem
    assert read_service_secret(tmp_path) is None, "the window must not be handed an exposed secret either"

    usage = tmp_path / "usage"
    usage.mkdir()
    app = create_app(db_path=tmp_path / "o.db", usage_directory=usage, poll_providers=False, write_secret=secret)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for method, route, body in _UNSAFE:
            response = client.request(method, route, json=body, headers={"X-ModelQuad-Token": first.value})
            assert response.status_code == 403, route
            assert response.json()["code"] == trust.TOKEN_UNAVAILABLE
            assert "another account" in response.json()["detail"]


@windows_only
def test_windows_secret_that_inherits_its_folders_access_is_refused(tmp_path):
    # A plain write inherits the folder's access list. Its owner depends on
    # the account: the user, or the Administrators group when elevated. The
    # access list is judged first, so the reason is the same under both.
    (tmp_path / auth.SECRET_FILE_NAME).write_text("x" * 43, encoding="ascii")
    secret = ensure_service_secret(tmp_path)
    assert not secret.usable
    assert "inherits" in secret.problem


@windows_only
def test_the_refusal_reason_does_not_depend_on_who_owns_the_file():
    # An elevated administrator's files default to BUILTIN\Administrators as
    # owner; the access list decides the reason whenever it is also wrong.
    user = "S-1-5-21-1-2-3-1001"
    admins = "S-1-5-32-544"
    private = ((0, 0, user), (0, 0, "S-1-5-18"))
    with pytest.raises(auth.SecretFileError, match="inherits"):
        auth.verify_descriptor(auth.SecurityDescription(admins, False, private), user)
    with pytest.raises(auth.SecretFileError, match="inherits"):
        auth.verify_descriptor(auth.SecurityDescription(admins, True, ((0, 0x10, user),)), user)
    with pytest.raises(auth.SecretFileError, match="another account"):
        auth.verify_descriptor(auth.SecurityDescription(admins, True, (*private, (0, 0, "S-1-1-0"))), user)
    # A correct, protected access list with the wrong owner is still refused.
    with pytest.raises(auth.SecretFileError, match="owned by another"):
        auth.verify_descriptor(auth.SecurityDescription(admins, True, private), user)


@windows_only
def test_verify_descriptor_rules():
    user = "S-1-5-21-1-2-3-1001"
    good = auth.SecurityDescription(user, True, ((0, 0, user), (0, 0, "S-1-5-18")))
    auth.verify_descriptor(good, user)
    for bad, words in (
        (auth.SecurityDescription("S-1-5-32-544", True, good.aces), "owned by another"),
        (auth.SecurityDescription(user, True, None), "everyone"),
        (auth.SecurityDescription(user, False, good.aces), "inherits"),
        (auth.SecurityDescription(user, True, ((0, 0x10, user),)), "inherits"),
        (auth.SecurityDescription(user, True, ((0, 0, user), (0, 0, "S-1-5-32-545"))), "another account"),
        (auth.SecurityDescription(user, True, ((1, 0, "S-1-1-0"),)), "unexpected"),
    ):
        with pytest.raises(auth.SecretFileError, match=words):
            auth.verify_descriptor(bad, user)


@windows_only
def test_protect_directory_makes_a_folder_and_its_contents_private(tmp_path):
    folder = tmp_path / "profile"
    (folder / "Default").mkdir(parents=True)
    (folder / "Default" / "History").write_text("http://127.0.0.1:8765/desktop#mq_token=x", encoding="utf-8")
    user = auth.current_user_sid()
    assert not auth.directory_is_private(auth.describe_path(folder), user)

    auth.protect_directory(folder)
    assert auth.directory_is_private(auth.describe_path(folder), user)
    # The folder itself: protected, and its own entries are explicit.
    _, flags, aces = _parse_sddl(_sddl(folder))
    assert "P" in flags
    assert all("ID" not in ace.split(";")[1] for ace in aces), aces
    # Its contents are deliberately *not* protected: every entry they carry is
    # inherited (ID) from the private folder, so they are exactly as private
    # as it is, and a later change to the folder reaches them too. An
    # inherited entry is the intended shape here, unlike on the secret file,
    # whose verification refuses one.
    for item in (folder / "Default", folder / "Default" / "History"):
        _, _flags, aces = _parse_sddl(_sddl(item))
        assert aces and all("ID" in ace.split(";")[1] for ace in aces), aces
        assert {_sid_value(ace.strip("()").split(";")[-1]) for ace in aces} == {user, "S-1-5-18"}, aces
    auth.protect_directory(folder)  # already private: a no-op, not a re-propagation


# --------------------------------------------------------------------------
# The value never leaves: health, doctor, the service's own output
# --------------------------------------------------------------------------

_UNSAFE = [
    ("PUT", "/api/routing-state", {"routing_mode": "manual"}),
    ("PUT", "/api/settings", {}),
    ("PUT", "/api/privacy", {}),
    ("PUT", "/api/providers/codex", {"enabled": True}),
    ("POST", "/api/route", {"task_type": "review", "prompt": "x"}),
    ("POST", "/api/repair", {}),
    ("POST", "/api/usage/refresh", {}),
]


def test_the_secret_is_not_in_health_or_the_pending_summary(tmp_path):
    from modelquad import paths

    secret = ensure_service_secret(paths.state_root())
    usage = tmp_path / "usage"
    usage.mkdir()
    app = create_app(db_path=tmp_path / "o.db", usage_directory=usage, poll_providers=False, write_secret=secret)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-ModelQuad-Token": secret.value}) as client:
        for route in ("/api/health", "/api/pending-output"):
            response = client.get(route)
            assert response.status_code == 200, route
            assert secret.value not in response.text, route


def test_the_secret_is_not_in_cli_doctor_output(capsys):
    from modelquad import paths

    secret = ensure_service_secret(paths.state_root())
    cli_module.main(["doctor"])
    out = capsys.readouterr()
    assert secret.value not in out.out + out.err


def _run_service(monkeypatch, command: str, managed_service_child) -> tuple[dict, list[str]]:
    import uvicorn

    import modelquad.service.app as service_app_module

    created: dict = {}
    opened: list[str] = []

    monkeypatch.setattr(service_app_module, "create_app", lambda **kwargs: created.update(kwargs) or object())
    def run(*a, **kwargs):
        assert kwargs["proxy_headers"] is False
    monkeypatch.setattr(uvicorn, "run", run)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    argv, _ = managed_service_child([command])
    assert cli_module.main(argv) == 0
    return created, opened


def test_the_service_command_creates_the_secret_and_requires_it(monkeypatch, capsys, managed_service_child):
    from modelquad import paths

    created, _ = _run_service(monkeypatch, "serve", managed_service_child)
    secret = created["write_secret"]
    assert isinstance(secret, ServiceSecret) and secret.usable
    assert read_service_secret(paths.state_root()) == secret.value
    out = capsys.readouterr()
    assert secret.value not in out.out + out.err, "the service's output is its log"


def test_an_unverifiable_secret_is_logged_without_its_value(monkeypatch, capsys, managed_service_child):
    from modelquad import paths
    from modelquad import windowless_diagnostics

    events = []
    monkeypatch.setattr(windowless_diagnostics, 'emit', lambda *args: events.append(args))

    root = paths.state_root()
    root.mkdir(parents=True, exist_ok=True)
    auth._create_exclusively(root / auth.SECRET_FILE_NAME, "short")
    created, opened = _run_service(monkeypatch, "serve", managed_service_child)
    assert not created["write_secret"].usable
    output = capsys.readouterr()
    assert ('backend_failed',) in events
    assert 'short' not in repr(events)
    assert "short" not in output.out + output.err
    assert opened == []
