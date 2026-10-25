# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The handoff approval widget, for a Claude Code that renders MCP Apps.

The desktop app's Code tab shows no MCP form, but it can render an MCP App
(a ``ui://`` page in a sandboxed frame) once Anthropic turns that on for the
account. Where the client says it renders them, ``modelquad_handoff`` shows
this page instead of the form:

1. Claude Code's own approval card comes first, as for every Codex call.
2. ``modelquad_handoff`` holds the request here and returns a pending id. The
   page shows what the server holds: the files with sizes and the account.
3. The page's Approve or Decline button calls ``modelquad_handoff_confirm``,
   a tool whose ``_meta.ui.visibility`` is ``["app"]``. A host keeps such a
   tool out of the model's tool list, so only a press in the page reaches it.
4. ``modelquad_handoff_wait`` starts Codex only after that press said yes.

Everything stays in this server process: nothing is written to disk, and a
request expires unanswered after :data:`PENDING_SECONDS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Any

URI = "ui://modelquad/handoff.html"
#: Off until a host is shown to deliver the page's Approve press. With it off,
#: modelquad_handoff carries no ``_meta.ui`` link, so no host draws a window
#: for it: the desktop app drew an empty "Rendering widget" frame beside its
#: approval card (live test, 2026-10-25), and its presses never arrived.
ENABLED = False
PENDING_SECONDS = 600.0
MAX_PENDING = 16


@dataclass
class Pending:
    arguments: dict[str, Any]
    created: float
    decision: str | None = None  # "approve" or "decline", set once by the page
    used: bool = False  # set once by the wait tool when it acts on the decision
    event: threading.Event = field(default_factory=threading.Event)


_LOCK = threading.Lock()
_PENDING: dict[str, Pending] = {}


def _expire(now: float) -> None:
    for key in [key for key, item in _PENDING.items() if now - item.created >= PENDING_SECONDS]:
        _PENDING.pop(key).event.set()


def hold(arguments: dict[str, Any]) -> str:
    """Keep a handoff until the page answers; return its single-use id."""
    now = time.monotonic()
    with _LOCK:
        _expire(now)
        while len(_PENDING) >= MAX_PENDING:
            _PENDING.pop(min(_PENDING, key=lambda key: _PENDING[key].created)).event.set()
        key = secrets.token_urlsafe(24)
        _PENDING[key] = Pending(dict(arguments), now)
        return key


def decide(key: str, approve: bool) -> str:
    """Record the page's answer once: "recorded", or why it was not."""
    with _LOCK:
        _expire(time.monotonic())
        item = _PENDING.get(key)
        if item is None:
            return "unknown_or_expired"
        if item.decision is not None:
            return "already_answered"
        item.decision = "approve" if approve else "decline"
        item.event.set()
        return "recorded"


def take(key: str, wait_seconds: float) -> tuple[str, dict[str, Any] | None]:
    """Wait for the page's answer, then use it up.

    Returns ("approve", arguments) once, ("decline", None), ("waiting", None)
    when no answer came within ``wait_seconds``, or ("unknown_or_expired", None).
    Blocking; run it off the event loop.
    """
    with _LOCK:
        _expire(time.monotonic())
        item = _PENDING.get(key)
        if item is None or item.used:
            return "unknown_or_expired", None
    item.event.wait(max(0.0, wait_seconds))
    with _LOCK:
        current = _PENDING.get(key)
        if current is not item or item.used:
            return "unknown_or_expired", None
        if item.decision is None:
            return "waiting", None
        item.used = True
        _PENDING.pop(key, None)
        return item.decision, (dict(item.arguments) if item.decision == "approve" else None)


def reset() -> None:
    """Drop every held request (tests)."""
    with _LOCK:
        for item in _PENDING.values():
            item.event.set()
        _PENDING.clear()


# The page. No network (the host's default CSP allows none), no outside fonts;
# it reads the host's theme variables when given and falls back to its own.
# It renders only what the tool result carries, as text, never as HTML.
HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ModelQuad handoff</title>
<style>
  :root {
    color-scheme: light dark;
    --mq-bg: var(--color-background-primary, light-dark(#ffffff, #1f1f1e));
    --mq-fg: var(--color-text-primary, light-dark(#1f1f1e, #f4f3ee));
    --mq-muted: var(--color-text-secondary, light-dark(#5f5e5a, #b4b2a9));
    --mq-line: var(--color-border-primary, light-dark(#e3e1d9, #3a3a37));
    --mq-soft: var(--color-background-secondary, light-dark(#f6f5f0, #2a2a28));
    --mq-accent: light-dark(#c15f3c, #e08a63);
    --mq-ok: light-dark(#2f7a4f, #7fcf9f);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--mq-bg); color: var(--mq-fg);
         font: 14px/1.5 var(--font-sans, system-ui, -apple-system, "Segoe UI", sans-serif); }
  .card { border: 1px solid var(--mq-line); border-radius: 12px; padding: 16px 18px; }
  .eyebrow { font-size: 12px; letter-spacing: .04em; color: var(--mq-muted); text-transform: uppercase; }
  h1 { font-size: 17px; margin: 4px 0 12px; }
  h2 { font-size: 12px; color: var(--mq-muted); margin: 14px 0 6px; font-weight: 600; }
  ul { list-style: none; margin: 0; padding: 0; border: 1px solid var(--mq-line); border-radius: 8px; }
  li { display: flex; justify-content: space-between; gap: 12px; padding: 7px 10px;
       border-top: 1px solid var(--mq-line); font-family: var(--font-mono, ui-monospace, Consolas, monospace);
       font-size: 12.5px; overflow-wrap: anywhere; }
  li:first-child { border-top: 0; }
  li span:last-child { color: var(--mq-muted); white-space: nowrap; }
  .note { background: var(--mq-soft); border-radius: 8px; padding: 8px 10px; margin-top: 10px; color: var(--mq-muted); }
  .task { white-space: pre-wrap; color: var(--mq-muted); max-height: 7.5em; overflow: auto; }
  .actions { display: flex; gap: 8px; margin-top: 16px; }
  button { font: inherit; border-radius: 8px; padding: 8px 16px; cursor: pointer; border: 1px solid var(--mq-line);
           background: transparent; color: var(--mq-fg); }
  button.primary { background: var(--mq-accent); border-color: var(--mq-accent); color: #fff; font-weight: 600; }
  button:disabled { opacity: .55; cursor: default; }
  button:focus-visible { outline: 2px solid var(--mq-accent); outline-offset: 2px; }
  .status { margin-top: 12px; font-weight: 600; }
  .status.ok { color: var(--mq-ok); }
</style>
</head>
<body>
<div class="card" id="card" hidden>
  <div class="eyebrow">ModelQuad</div>
  <h1>Hand this task to Codex?</h1>
  <h2>Files to send</h2>
  <ul id="files"></ul>
  <div class="note" id="account"></div>
  <h2>Task</h2>
  <div class="task" id="task"></div>
  <div class="actions">
    <button class="primary" id="approve">Approve</button>
    <button id="decline">Decline</button>
  </div>
  <div class="status" id="status" role="status"></div>
</div>
<script>
(() => {
  const parent = window.parent;
  let nextId = 1, pendingId = null, ready = false;
  const waiting = new Map();
  const $ = (id) => document.getElementById(id);

  function send(method, params) {
    const id = nextId++;
    parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    return new Promise((resolve) => waiting.set(id, resolve));
  }

  function show(result) {
    const data = (result && result.structuredContent) || {};
    if (typeof data.pending_id !== "string") return;
    pendingId = data.pending_id;
    const list = $("files");
    list.textContent = "";
    const files = Array.isArray(data.sent_files_preview) ? data.sent_files_preview : [];
    if (!files.length) {
      const li = document.createElement("li");
      li.textContent = "None: only the task text is sent.";
      list.appendChild(li);
    }
    for (const file of files) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      const size = document.createElement("span");
      name.textContent = String(file.path);
      size.textContent = String(file.bytes) + " bytes";
      li.append(name, size);
      list.appendChild(li);
    }
    $("account").textContent = String(data.codex_account || "Codex account: unknown.");
    $("task").textContent = String(data.task_preview || "");
    $("card").hidden = false;
  }

  async function answer(approve) {
    if (!pendingId) return;
    $("approve").disabled = $("decline").disabled = true;
    const reply = await send("tools/call", {
      name: "modelquad_handoff_confirm", arguments: { pending_id: pendingId, approve }
    });
    const outcome = reply && reply.result && reply.result.structuredContent;
    const status = $("status");
    if (outcome && outcome.recorded) {
      status.className = "status ok";
      status.textContent = approve ? "Approved. Claude starts Codex now." : "Declined. Nothing was sent.";
    } else if (outcome && outcome.outcome === "unknown_or_expired") {
      status.className = "status";
      status.textContent = "This request is no longer open. Nothing was sent.";
    } else if (outcome && outcome.outcome === "already_answered") {
      status.className = "status";
      status.textContent = "This request was already answered.";
    } else {
      status.className = "status";
      status.textContent = "Your answer did not reach ModelQuad, so nothing was sent. Answer in the conversation instead.";
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && waiting.has(message.id)) {
      waiting.get(message.id)(message);
      waiting.delete(message.id);
      return;
    }
    if (ready && message.method === "ui/notifications/tool-result") show(message.params);
  });

  $("approve").addEventListener("click", () => answer(true));
  $("decline").addEventListener("click", () => answer(false));

  send("ui/initialize", {
    protocolVersion: "2026-01-26",
    appInfo: { name: "modelquad-handoff", version: "1" },
    appCapabilities: {}
  }).then(() => {
    ready = true;
    parent.postMessage({ jsonrpc: "2.0", method: "ui/notifications/initialized" }, "*");
  });
})();
</script>
</body>
</html>
"""
