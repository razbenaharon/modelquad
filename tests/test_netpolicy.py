# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""ModelQuad's own process connects only to loopback, enforced by an audit hook."""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from modelquad import netpolicy


@pytest.mark.parametrize("address", [
    ("127.0.0.1", 8765), ("127.5.6.7", 1), ("::1", 8765, 0, 0), ("[::1]", 80),
    ("localhost", 8765), ("LOCALHOST", 1), ("::ffff:127.0.0.1", 80), (b"127.0.0.1", 1),
    r"\\.\pipe\x", "/tmp/socket",
])
def test_loopback_addresses_are_local(address):
    assert netpolicy.is_loopback_address(address)


@pytest.mark.parametrize("address", [
    ("192.0.2.1", 443), ("8.8.8.8", 53), ("2001:db8::1", 443, 0, 0), ("example.com", 443),
    ("api.openai.com", 443), ("0.0.0.0", 80), (), None, (None, 1),
])
def test_other_addresses_are_not(address):
    assert not netpolicy.is_loopback_address(address)


def test_the_guard_refuses_a_non_loopback_connect_and_allows_loopback():
    netpolicy.install_loopback_guard()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(netpolicy.OutboundConnectionRefused):
                sock.connect(("192.0.2.1", 9))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            with pytest.raises(netpolicy.OutboundConnectionRefused):
                sock.sendto(b"x", ("192.0.2.1", 9))
        # Loopback still works: a listener on 127.0.0.1 accepts the connection.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5):
                pass
    finally:
        netpolicy.guard_active = False


def test_the_guard_is_installed_once_and_switchable():
    netpolicy.install_loopback_guard()
    netpolicy.install_loopback_guard()
    netpolicy.guard_active = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # Switched off: the hook no longer refuses (UDP sendto needs no peer).
        sock.sendto(b"x", ("192.0.2.1", 9))


def test_a_child_process_is_not_restricted():
    """The guard covers ModelQuad's own process; programs it starts are separate."""

    netpolicy.install_loopback_guard()
    try:
        code = ("import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
                "s.sendto(b'x', ('192.0.2.1', 9)); print('sent')")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.stdout.strip() == "sent", result.stderr
    finally:
        netpolicy.guard_active = False


def test_entry_points_install_the_guard():
    import inspect

    from modelquad import cli_entry
    from modelquad.jobs import runner
    from modelquad.mcp import server

    for function in (cli_entry.main, server.main, runner.main):
        assert "install_loopback_guard()" in inspect.getsource(function), function


def test_an_api_execution_provider_is_refused_before_routing(monkeypatch):
    from modelquad import registry

    monkeypatch.setattr(registry, "api_execution_providers", lambda registry=None: ("vendor",))
    assert netpolicy.providers_refused_by_policy() == {"vendor": netpolicy.LOOPBACK_ONLY_REASON}
    monkeypatch.setattr(registry, "api_execution_providers", lambda registry=None: ())
    assert netpolicy.providers_refused_by_policy() == {}


def test_modelquad_code_makes_none_of_the_calls_the_guard_cannot_see():
    # NETWORK.md and SECURITY.md say the hook does not see asyncio Proactor
    # connects or host name lookups, and that ModelQuad's own code makes
    # neither. Keep that last claim true: a new call of this kind needs the
    # guard widened, or the documents changed, first.
    from pathlib import Path
    import re

    unseen = re.compile(
        r"\b(?:open_connection|create_connection|sock_connect|create_datagram_endpoint"
        r"|getaddrinfo|gethostbyname(?:_ex)?|getnameinfo)\s*\("
    )
    src = Path(netpolicy.__file__).resolve().parent
    found = []
    for path in sorted(src.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if unseen.search(line):
                found.append(f"{path.relative_to(src).as_posix()}: {line.strip()}")
    # The one outbound connect names the literal loopback address, so the
    # lookup inside socket.create_connection makes no resolver query.
    assert found == [
        'service/listener.py: sock = socket.create_connection(("127.0.0.1", port),'
        " timeout=_remaining(deadline, 0.75))"
    ], found
