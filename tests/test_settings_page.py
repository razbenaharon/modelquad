# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one-time settings page: loopback only, its secret on every request, no other site."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from modelquad import plans, repo_settings, settings_page
from modelquad.adapters.server_instructions import read_proactive


@pytest.fixture
def page(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    running = settings_page.SettingsPage(project.resolve(), tmp_path / "usage")
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("ended", running.serve()), daemon=True)
    thread.start()
    yield running
    running.finish()
    thread.join(timeout=10)


def _request(page, method, path, *, body=None, headers=None, host=None):
    connection = http.client.HTTPConnection(settings_page.HOST, page.port, timeout=10)
    sent = {"Host": host or f"{settings_page.HOST}:{page.port}"}
    sent.update(headers or {})
    data = None if body is None else json.dumps(body).encode("utf-8")
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def _post(page, path, body=None, **extra):
    headers = {"Content-Type": "application/json", "X-ModelQuad-Token": page.token}
    headers.update(extra.pop("headers", {}))
    return _request(page, "POST", path, body=body if body is not None else {}, headers=headers, **extra)


def test_it_listens_on_loopback_only_on_a_port_the_system_picks(page):
    assert page.server.server_address[0] == "127.0.0.1"
    assert page.port > 0
    assert page.url == f"http://127.0.0.1:{page.port}/s/{page.token}"
    assert len(page.token) >= 40


def test_the_page_needs_its_secret_and_ships_a_strict_policy(page):
    response, body = _request(page, "GET", f"/s/{page.token}")
    assert response.status == 200
    policy = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    nonce = policy.split("'nonce-")[1].split("'")[0]
    text = body.decode("utf-8")
    assert f'<script nonce="{nonce}">' in text and "{{NONCE}}" not in text
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Frame-Options") == "DENY"
    # Nothing on the page is fetched from anywhere else.
    assert "https://" not in text and "http://" not in text
    for path in ("/", "/s/", f"/s/{page.token}x", "/s/" + "a" * len(page.token)):
        assert _request(page, "GET", path)[0].status == 404


@pytest.mark.parametrize("host", ["localhost", "evil.example:80", "127.0.0.1", "127.0.0.1:1"])
def test_a_request_naming_another_host_is_refused(page, host):
    # A DNS name rebound to 127.0.0.1 arrives with its own Host header.
    assert _request(page, "GET", f"/s/{page.token}", host=host)[0].status == 404
    assert _post(page, "/api/state", host=host)[0].status == 404


def test_reading_and_changing_need_the_secret_in_a_header(page):
    for headers in ({"X-ModelQuad-Token": "wrong"}, {"X-ModelQuad-Token": ""}):
        assert _post(page, "/api/state", headers=headers)[0].status == 404
    response = _request(page, "POST", "/api/set",
                        body={"setting": "proactive", "value": "on"},
                        headers={"Content-Type": "application/json"})[0]
    assert response.status == 404
    assert read_proactive() is False


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Content-Type": "text/plain"},
    {"Content-Type": "application/x-www-form-urlencoded"},
])
def test_another_site_cannot_change_a_setting(page, headers):
    response, _ = _post(page, "/api/set", {"setting": "proactive", "value": "on"}, headers=headers)
    assert response.status in (404, 415)
    assert read_proactive() is False


def test_a_preflight_is_never_agreed_to(page):
    response, _ = _request(page, "OPTIONS", "/api/set", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-modelquad-token"})
    assert response.status == 405
    assert response.getheader("Access-Control-Allow-Origin") is None


def test_the_page_reads_and_writes_the_same_settings_as_the_commands(page):
    response, body = _post(page, "/api/state")
    state = json.loads(body)
    assert response.status == 200
    assert state["proactive"] is False
    assert state["project"]["mode"] == "inherit"
    assert state["plans"]["claude"]["choices"] == list(plans.DECLARABLE_PLANS["claude"])

    origin = {"Origin": page.origin, "Sec-Fetch-Site": "same-origin"}
    state = json.loads(_post(page, "/api/set", {"setting": "proactive", "value": "on"}, headers=origin)[1])
    assert state["proactive"] is True and read_proactive() is True

    state = json.loads(_post(page, "/api/set", {"setting": "repo", "value": "off"})[1])
    assert state["project"]["mode"] == "off"
    assert repo_settings.read_mode(page.project) == "off"
    assert "refuses every Codex call" in state["project"]["in_effect"]

    state = json.loads(_post(page, "/api/set", {"setting": "plan_codex", "value": "plus"})[1])
    assert state["plans"]["codex"]["declared"] == "plus"
    assert plans.read_declared()["codex"] == "plus"
    _post(page, "/api/set", {"setting": "plan_codex", "value": "unset"})
    assert plans.read_declared()["codex"] is None


@pytest.mark.parametrize("change", [
    {"setting": "proactive", "value": "yes"},
    {"setting": "repo", "value": "always"},
    {"setting": "plan_claude", "value": "gold"},
    {"setting": "job_runner", "value": "on"},
    {"setting": "proactive", "value": True},
    {"setting": "proactive", "value": "on", "extra": 1},
    ["proactive", "on"],
])
def test_an_unknown_change_writes_nothing(page, change):
    response, body = _post(page, "/api/set", change)
    assert response.status == 400 and json.loads(body)["error"]
    assert read_proactive() is False
    assert repo_settings.read_mode(page.project) is None
    assert plans.read_declared() == {"claude": None, "codex": None}


def test_a_large_body_is_refused(page):
    response, _ = _post(page, "/api/set", {"setting": "proactive", "value": "on" + " " * 5000})
    assert response.status == 413
    assert read_proactive() is False


def test_done_ends_the_server(tmp_path):
    running = settings_page.SettingsPage(None, tmp_path)
    ended = {}
    thread = threading.Thread(target=lambda: ended.setdefault("how", running.serve()), daemon=True)
    thread.start()
    assert _post(running, "/api/done")[0].status == 200
    thread.join(timeout=10)
    assert ended["how"] == "done"
    with pytest.raises(OSError):
        _post(running, "/api/state")


def test_it_closes_itself_when_idle(tmp_path):
    now = [0.0]
    running = settings_page.SettingsPage(None, tmp_path, idle_seconds=60, clock=lambda: now[0])
    now[0] = 61.0
    assert running.serve() == "idle"


def test_without_a_project_the_page_offers_only_the_general_settings(tmp_path):
    state = settings_page.current_state(None, tmp_path)
    assert state["project"] is None
    with pytest.raises(settings_page.ChangeRefused):
        settings_page.apply_change({"setting": "repo", "value": "on"}, None)


def test_the_cli_opens_the_page_and_returns_when_done(tmp_path, monkeypatch, capsys):
    from modelquad import cli

    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_browser(url):
        seen["url"] = url

        def press_done():
            port = int(url.split(":")[2].split("/")[0])
            token = url.rsplit("/", 1)[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("POST", "/api/done", body=b"{}", headers={
                "Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                "X-ModelQuad-Token": token})
            connection.getresponse().read()

        threading.Timer(0.3, press_done).start()
        return True

    assert cli._run_settings_page(None, open_browser=fake_browser) == 0
    out = capsys.readouterr().out
    assert "open in your browser" in out and "Settings page closed" in out
    # The secret is not printed when the browser opened.
    assert seen["url"].rsplit("/", 1)[1] not in out


def test_the_cli_refuses_a_missing_project_folder(tmp_path, capsys):
    from modelquad import cli

    assert cli.main(["settings", "open", "--project", str(tmp_path / "missing"), "--no-browser"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_page_is_packaged():
    text = Path(settings_page.__file__).with_name("settings_page.html").read_text(encoding="utf-8")
    assert "{{NONCE}}" in text
    root = Path(__file__).resolve().parents[1]
    assert '"settings_page.html"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def _raw(page, data: bytes) -> bytes:
    import socket

    with socket.create_connection((settings_page.HOST, page.port), timeout=10) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def test_a_refused_requests_body_is_never_read_as_a_second_request(page):
    # A cross-site text/plain POST whose body is a complete authenticated request.
    inner = json.dumps({"setting": "proactive", "value": "on"}).encode("utf-8")
    smuggled = (f"POST /api/set HTTP/1.1\r\nHost: 127.0.0.1:{page.port}\r\n"
                f"X-ModelQuad-Token: {page.token}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(inner)}\r\n\r\n").encode("ascii") + inner
    outer = (f"POST /api/set HTTP/1.1\r\nHost: 127.0.0.1:{page.port}\r\n"
             "Origin: https://evil.example\r\nContent-Type: text/plain\r\n"
             f"Content-Length: {len(smuggled)}\r\n\r\n").encode("ascii") + smuggled
    reply = _raw(page, outer)
    assert reply.count(b"HTTP/1.0 ") == 1 and reply.startswith(b"HTTP/1.0 404")
    assert read_proactive() is False


def test_a_chunked_body_is_refused(page):
    reply = _raw(page, (f"POST /api/state HTTP/1.1\r\nHost: 127.0.0.1:{page.port}\r\n"
                        f"X-ModelQuad-Token: {page.token}\r\nContent-Type: application/json\r\n"
                        "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n").encode("ascii"))
    assert reply.startswith(b"HTTP/1.0 411")


def test_done_needs_the_secret(page):
    assert _request(page, "POST", "/api/done", body={},
                    headers={"Content-Type": "application/json"})[0].status == 404
    assert _post(page, "/api/state")[0].status == 200


@pytest.mark.parametrize("path", ["/s/{T}?x", "/s/{T}/", "/s/%7BT%7D", "/s/{T}%00",
                                  "http://127.0.0.1/s/{T}"])
def test_path_quirks_do_not_reach_the_page(page, path):
    assert _request(page, "GET", path.replace("{T}", page.token))[0].status == 404


def test_post_paths_must_match_exactly(page):
    assert _post(page, "/api/state?x")[0].status == 404
    assert _post(page, "/api/set/", {"setting": "proactive", "value": "on"})[0].status == 404
    assert read_proactive() is False


def test_nothing_is_answered_once_the_page_is_closing(page):
    page.closing = True
    assert _post(page, "/api/set", {"setting": "proactive", "value": "on"})[0].status == 404
    assert read_proactive() is False
    page.closing = False


def test_a_slow_connection_does_not_hold_a_thread_forever(page):
    handler = page.server.RequestHandlerClass
    assert handler.timeout == settings_page._HANDLER_TIMEOUT_SECONDS <= 30
    assert handler.protocol_version == "HTTP/1.0"
