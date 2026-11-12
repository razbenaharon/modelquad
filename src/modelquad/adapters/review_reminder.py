# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The review reminder and the review gate: Claude Code hooks that make Codex reviews happen.

Claude tends to forget an instruction it read at session start while it is
deep in a task (recorded trials of fresh sessions, 2026-11-06). So this
module counts the different files Claude changes in a session, under the
session's own folder, and acts at three moments:

* **PostToolUse**, advisory, everywhere: once ``THRESHOLD`` files changed
  without a Codex review since, it adds one line of context when Claude next
  runs a command (usually the tests, at the end of the change), at most
  ``MAX_REMINDERS`` times, and only after further edits since the last one.
* **PreToolUse**, the consultation gate: in a folder with the standing grant,
  before the first edit of a sensitive file (authentication, cryptography,
  permissions, secrets, migrations, installers, CI workflows), or the edit
  that would make the change three files, with no Codex consultation in this
  change yet, it stops that one edit once and asks Claude to consult Codex on
  the approach first. It never stops a change twice; after a refused or
  failed consultation Claude goes on and says so. Without a grant it only
  adds one line of context before a sensitive file, in "decide" mode.
* **Stop**, the review gate, only in a folder where the user turned the
  standing grant on (``grant.py``): when Claude stops after a change of
  ``THRESHOLD`` files (or one sensitive file) with no Codex review since, it
  blocks once, telling Claude to run the review now. The two gates are the
  exceptions in AGENTS.md invariant 6: at most one stop per change set each,
  never while Claude is already continuing because of a stop hook, and only
  to ask for Codex. If Claude stops again without one, or the review failed,
  the gate does not block again: it tells the user directly
  (``systemMessage``) that the change was not reviewed.

Once the gate has told the user, that change set is closed. A further edit to
one of its files is a follow-up to it and stops nothing again. An edit to a
file outside it starts a new change set, which gets its own consultation gate
and review gate.

A review (``modelquad_delegate`` with task_type review or trigger
review_gate) that Codex answered starts the count again; one that was refused
or failed, or that returned only the first-use notice, marks the review
failed. A reply that is only the handle of a background job is neither: the
review is pending, the gate does not ask for it again, and the user is told
once that the change is not reviewed yet. Any other answered call counts as
the change's consultation. A gate counts as used only once its message was
written: a run that ran out of time before that leaves the gate as it was.
The hook never approves
or denies a tool call, never opens the transcript path Claude Code hands it,
starts no process and never starts Codex.

What it keeps: one small file per session under ``<state>/review-reminder/``,
named by a digest of the session id, holding SHA-256 digests of the changed
file paths (never the paths), whether one was sensitive, and the gate's
state; files older than two days are removed. Gate events go to the review
ledger (``review_ledger.py``) with digests of the folder and the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

#: A Write call carries the whole file, so the cap is larger than the startup hook's.
BYTE_CAP = 8 * 1024 * 1024
HOOK_BUDGET = 1.5
THRESHOLD = 3
MAX_REMINDERS = 2
MAX_DIGESTS = 64
KEEP_SECONDS = 2 * 24 * 3600
FOLDER_NAME = "review-reminder"
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
#: Running a command after the edits is usually the end of a change: the tests.
RUN_TOOLS = frozenset({"Bash", "PowerShell"})
DELEGATE_TOOL_SUFFIX = "modelquad_delegate"

#: Paths whose change calls for a consultation first and a review at the end,
#: even when it is one file. Matched against the path relative to the session
#: folder, case-insensitively.
SENSITIVE = re.compile(
    r"(^|/)(\.github/workflows/|[^/]*(auth|login|password|passwd|secret|token|credential|crypt|"
    r"permission|security|migration|migrate|install|deploy)[^/]*(/|$))|\.(ps1|pem|key)$",
    re.IGNORECASE)

DECIDE = (
    "ModelQuad: you have changed {count} files in this session and Codex has not reviewed "
    "them. Before your final message on this change, call `modelquad_delegate` for a "
    "review (trigger review_gate), with the changed files in `files`. If the user or "
    "Claude Code declines, carry on without Codex. A change across this many files is not "
    "a small one, so this applies to it.")
OFFER = (
    "ModelQuad: you have changed {count} files in this session and Codex has not reviewed "
    "them. In your final message on this change, offer the user a Codex review in one "
    "short sentence. A change across this many files is not a small one, so this "
    "applies to it.")
CONSULT = (
    "ModelQuad: you are about to change a sensitive file ({kind}). If this change is large, "
    "architectural or security-relevant, consult Codex first: call `modelquad_delegate` "
    "with task_type architecture, trigger pre_change and the files involved, verify its "
    "advice, then continue. Skip it for a trivial edit.")
CONSULT_GATE = (
    "ModelQuad consultation gate: {why}, and Codex has not been consulted on this change. "
    "Before this edit, call `modelquad_delegate` with task_type architecture, trigger "
    "pre_change, your plan in the prompt and the files involved in `files`. Check its advice "
    "against the code and the requirements, then make this edit again. If the call is "
    "refused or Codex is unavailable, say so and go on: this gate stops a change only once.")
GATE = (
    "ModelQuad review gate: this session changed {count} file(s){sensitive} and Codex has "
    "not reviewed them. Before you finish, call `modelquad_delegate` with task_type review, "
    "trigger review_gate and the changed files in `files`. Verify each finding against the "
    "code or a test before changing anything, record your decisions with "
    "`modelquad_review_verdict`, and say in your final message what the review found. If "
    "the call fails or Codex is unavailable, say plainly that the review was not done. Do "
    "not run Codex any other way.")
NOT_DONE = ("ModelQuad: Codex did not review this change ({count} file(s)). The review gate "
            "asked for one and none was done.")
#: The gate never asked (Claude was already continuing for a stop hook), so it says only that.
NOT_REVIEWED = "ModelQuad: Codex did not review this change ({count} file(s))."
PENDING = ("ModelQuad: the Codex review of this change ({count} file(s)) is still running in "
           "the background, so the change has not been reviewed by Codex yet.")
FAILED = ("ModelQuad: the Codex review of this change did not complete ({count} file(s)), "
          "so it was not reviewed by Codex.")

_EMPTY = {"files": [], "reminders": 0, "edits_since": 0, "sensitive": False,
          "consulted": False, "consult_pending": False, "consult_gate": False,
          "consult_reminded": False, "skip_logged": 0, "gate": "none", "review": "none"}
_CHANGE_RESET = {"files": [], "edits_since": 0, "reminders": 0, "sensitive": False,
                 "consulted": False, "consult_pending": False, "consult_gate": False,
                 "consult_reminded": False, "skip_logged": 0, "gate": "none", "review": "none"}
_GATES = ("none", "blocked", "reported")
#: ``pending``: a background job was started for the review and its answer has not been seen.
_REVIEWS = ("none", "done", "fixups", "failed", "pending")
_EVENTS = ("PostToolUse", "PreToolUse", "Stop")


def _read_event(stdin, clock, deadline):
    incoming = queue.Queue(maxsize=1)

    def read_input():
        try:
            incoming.put(stdin.read(BYTE_CAP + 1))
        except Exception:
            incoming.put(None)

    threading.Thread(target=read_input, daemon=True).start()
    raw = incoming.get(timeout=max(0, deadline - clock()))
    if not isinstance(raw, (bytes, str)):
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > BYTE_CAP:
        return None
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("hook_event_name") not in _EVENTS:
        return None
    return event


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))


def _relative(event) -> str | None:
    """The changed file's path relative to the session folder; None when outside it.

    A file outside the folder (a scratch file, another project) is not part of
    the change under review, so it is not counted.
    """
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    cwd = event.get("cwd")
    if not isinstance(path, str) or not path or len(path) > 4096:
        return None
    if not isinstance(cwd, str) or not cwd:
        return None
    full, root = _norm(path), _norm(cwd)
    if not os.path.isabs(full):
        full = _norm(os.path.join(root, full))
    try:
        if os.path.commonpath([full, root]) != root:
            return None
    except ValueError:  # different drives
        return None
    return os.path.relpath(full, root).replace(os.sep, "/")


def _count(value) -> int:
    return value if type(value) is int and 0 <= value <= 1000 else 0


def _load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_EMPTY, files=[])
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        return dict(_EMPTY, files=[])
    return {"files": files[:MAX_DIGESTS], "reminders": _count(payload.get("reminders")),
            "edits_since": _count(payload.get("edits_since")),
            "sensitive": payload.get("sensitive") is True,
            "consulted": payload.get("consulted") is True,
            "consult_pending": payload.get("consult_pending") is True,
            "consult_gate": payload.get("consult_gate") is True,
            "consult_reminded": payload.get("consult_reminded") is True,
            "skip_logged": _count(payload.get("skip_logged")),
            "gate": payload.get("gate") if payload.get("gate") in _GATES else "none",
            "review": payload.get("review") if payload.get("review") in _REVIEWS else "none"}


def _save(path: Path, payload: dict) -> None:
    from modelquad.adapters.atomicwrite import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, sort_keys=True) + "\n")


def _prune(folder: Path, now: float) -> None:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.suffix == ".json" and now - entry.stat().st_mtime > KEEP_SECONDS:
                entry.unlink()
        except OSError:
            pass


def _new_set(payload: dict, digest: str) -> bool:
    """Whether an edit of this file starts a new change set after a reported one.

    A reported change set is closed: editing one of its files again is a
    follow-up and stays reported, so neither gate stops it a second time.
    """
    return payload["gate"] == "reported" and digest not in payload["files"]


def _codex_allowed_mode(cwd) -> str | None:
    """'decide' or 'offer' for this folder, or None when Codex is off or only on request."""
    from modelquad import repo_settings
    from modelquad.adapters.server_instructions import read_proactive
    from modelquad.localpath import reason as local_reason

    proactive = read_proactive()
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute() or local_reason(cwd):
        return "decide" if proactive else "offer"
    user_mode = repo_settings.read_mode(Path(cwd))
    result = repo_settings.effective(global_proactive=proactive, user_mode=user_mode,
                                     policy=repo_settings.local_policy(Path(cwd)))
    if result.refusal is not None:
        return None
    return {repo_settings.PROACTIVE: "decide", repo_settings.OFFER: "offer"}.get(result.offer)


def _granted(cwd) -> bool:
    from modelquad import grant
    from modelquad.localpath import reason as local_reason

    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute() or local_reason(cwd):
        return False
    return grant.allows(cwd)


_FAILED_HEADLINES = ("Codex was not started", "Codex answer withheld", "Codex did not finish",
                     "Codex did not return in time")
_PENDING_HEADLINE = "Codex is working on this in the background"
_PRODUCT_PREFIX = "ModelQuad: "
ANSWERED, PENDING_REPLY, NOT_ANSWERED = "answered", "pending", "failed"


def _refused_payload(payload: dict) -> bool:
    return payload.get("refused") is True or payload.get("reason") == "no_standing_grant"


def _reply_data(response):
    """(the reply's data or None, its first text or None)."""
    content = response
    if isinstance(response, dict):
        for key in ("structuredContent", "structured_content"):
            if isinstance(response.get(key), dict):
                return response[key], None
        content = response.get("content")
    texts = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [block["text"] for block in content if isinstance(block, dict)
                 and block.get("type") == "text" and isinstance(block.get("text"), str)]
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data, texts[0]
    return None, (texts[0] if texts else None)


def _reply_state(response) -> str:
    """What a modelquad_delegate reply delivered: an answer, a pending job, or neither.

    Decided from the reply's data when the host passes it, otherwise from the
    reply's first line only (the outcome line, ``server._plain_result``):
    Codex's own answer text may quote a refusal marker, and that is no refusal.
    Only a reply that carries Codex's answer is ``answered``. The handle of a
    background job is ``pending``: Codex has not answered yet. A refusal, the
    first-use notice and a run without an answer are ``failed``.
    """
    data, text = _reply_data(response)
    if data is not None:
        if _refused_payload(data) or data.get("status") == "notice_first":
            return NOT_ANSWERED
        if "job_id" in data:
            return PENDING_REPLY
        if "output" in data:
            output = data["output"]
            return ANSWERED if isinstance(output, str) and output.strip() else NOT_ANSWERED
        return ANSWERED
    if text is None:
        # A shape this hook does not know: the old whole-reply search, which
        # errs toward "failed".
        try:
            text = response if isinstance(response, str) else json.dumps(response)
        except (TypeError, ValueError):
            return NOT_ANSWERED
        if _PENDING_HEADLINE in text or '"job_id"' in text:
            return PENDING_REPLY
        failed = any(marker in text for marker in (
            '"refused": true', '"refused":true', '"notice_first"', *_FAILED_HEADLINES,
            "no_standing_grant"))
        return NOT_ANSWERED if failed else ANSWERED
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first.startswith(_PRODUCT_PREFIX):
        first = first[len(_PRODUCT_PREFIX):]
    if first.startswith(_PENDING_HEADLINE):
        return PENDING_REPLY
    return NOT_ANSWERED if any(first.startswith(marker) for marker in _FAILED_HEADLINES) else ANSWERED


def _review_failed(response) -> bool:
    """Whether a modelquad_delegate reply says Codex did not answer (and none is pending)."""
    return _reply_state(response) == NOT_ANSWERED


def _is_review(tool_input) -> bool:
    if not isinstance(tool_input, dict):
        return False
    return tool_input.get("task_type") == "review" or tool_input.get("trigger") == "review_gate"


def _gate_event(cwd, session, event, files) -> None:
    from modelquad import review_ledger

    review_ledger.record_gate_event(cwd, session, event, files)


def _context(event_name: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                              "additionalContext": text}},
                      separators=(",", ":")) + "\n"


def _stop(event, payload, cwd, session, standing) -> tuple[str | None, str | None]:
    """The review gate's answer at Stop, and the gate event to record once it was shown.

    The answer is a block, a message to the user, or None. A gate event is
    returned, not recorded: the caller records it only after the answer was
    written, so a gate whose message nobody saw is not counted as used.
    """
    if payload["review"] in ("fixups", "done"):
        # The change was reviewed (and these edits fixed its findings). Close it;
        # later edits are a new change with its own consultation and review.
        payload.update(_CHANGE_RESET, files=[])
        return None, None
    count = len(payload["files"])
    pending = count >= THRESHOLD or (payload["sensitive"] and count >= 1)
    if not pending:
        if count and payload["skip_logged"] != count and (standing or _granted)(cwd):
            payload["skip_logged"] = count
            _gate_event(cwd, session, "skipped_small", count)
        return None, None
    if not (standing or _granted)(cwd):
        return None, None
    if payload["review"] in ("failed", "pending"):
        if payload["gate"] == "reported":
            return None, None
        payload["gate"] = "reported"
        if payload["review"] == "failed":
            return json.dumps({"systemMessage": FAILED.format(count=count)}) + "\n", "review_failed"
        # A background job has the review: never a second ask, and never "done".
        return json.dumps({"systemMessage": PENDING.format(count=count)}) + "\n", "review_pending"
    if payload["gate"] == "none" and event.get("stop_hook_active") is not True:
        payload["gate"] = "blocked"
        reason = GATE.format(count=count, sensitive=", including a sensitive one"
                             if payload["sensitive"] else "")
        return json.dumps({"decision": "block", "reason": reason}) + "\n", "blocked"
    if payload["gate"] != "reported":
        # "Asked for one" only when the gate did ask; a Stop first seen while
        # Claude continues for another stop hook was never asked anything.
        message = NOT_DONE if payload["gate"] == "blocked" else NOT_REVIEWED
        payload["gate"] = "reported"
        return json.dumps({"systemMessage": message.format(count=count)}) + "\n", "not_done_reported"
    return None, None


def run_hook(host, stdin, stdout, *, environ=None, clock=time.monotonic, state=None,
             mode=None, standing=None, now=time.time):
    """Fail silent: any failure prints nothing and exits 0."""
    environ = os.environ if environ is None else environ
    if environ.get("MODELQUAD_WORKER") == "1" or host != "claude":
        return 0
    deadline = clock() + HOOK_BUDGET
    try:
        event = _read_event(stdin, clock, deadline)
        if event is None:
            return 0
        name = event["hook_event_name"]
        tool = event.get("tool_name")
        session = event.get("session_id")
        cwd = event.get("cwd")
        if not isinstance(session, str) or not session:
            return 0
        if name != "Stop" and not isinstance(tool, str):
            return 0
        is_delegate = isinstance(tool, str) and tool.endswith(DELEGATE_TOOL_SUFFIX)
        if name != "Stop" and tool not in EDIT_TOOLS and tool not in RUN_TOOLS and not is_delegate:
            return 0
        if state is None:
            from modelquad.paths import state_root
            state = state_root()
        folder = Path(state) / FOLDER_NAME
        from modelquad.review_ledger import session_pseudonym
        record = folder / f"{session_pseudonym(session, Path(state), length=32)}.json"
        output = None
        # For a gate: the state to put back if its message is not written, and
        # the gate event to record once it was.
        unused = shown_event = None

        if name == "Stop":
            if not record.exists():
                return 0
            payload = _load(record)
            before = dict(payload, files=list(payload["files"]))
            output, event_name = _stop(event, payload, cwd, session, standing)
            _save(record, payload)
            if output is not None:
                unused, shown_event = before, (event_name, len(before["files"]))
        elif name == "PreToolUse":
            if tool not in EDIT_TOOLS:
                return 0
            relative = _relative(event)
            if relative is None:
                return 0
            payload = _load(record) if record.exists() else dict(_EMPTY, files=[])
            digest = _digest(relative.casefold())
            if _new_set(payload, digest):
                payload = dict(_CHANGE_RESET, files=[])
            if (payload["consulted"] or payload["consult_pending"]
                    or payload["review"] in ("done", "fixups", "pending")):
                return 0
            match = SENSITIVE.search(relative)
            grows = digest not in payload["files"] and len(payload["files"]) >= THRESHOLD - 1
            if match is None and not grows:
                return 0
            if (standing or _granted)(cwd):
                if payload["consult_gate"]:
                    return 0  # This change was stopped once already.
                unused = dict(payload, files=list(payload["files"]))
                shown_event = ("consult_blocked", len(payload["files"]))
                # Saved before the message, so a gate is never shown twice; put
                # back below if the message is not written after all.
                payload["consult_gate"] = True
                _save(record, payload)
                why = ("this edit changes a sensitive file" if match is not None
                       else f"this edit makes the change {len(payload['files']) + 1} files")
                output = json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": CONSULT_GATE.format(why=why)}},
                    separators=(",", ":")) + "\n"
            else:
                if match is None or payload["consult_reminded"]:
                    return 0
                if (mode or _codex_allowed_mode)(cwd) != "decide":
                    return 0
                payload["consult_reminded"] = True
                _save(record, payload)
                _gate_event(cwd, session, "consult_reminded", None)
                kind = match.group(0).strip("/") or relative
                output = _context("PreToolUse", CONSULT.format(kind=kind[:60]))
        elif is_delegate:
            payload = _load(record) if record.exists() else dict(_EMPTY, files=[])
            reply = _reply_state(event.get("tool_response"))
            if _is_review(event.get("tool_input")):
                if reply == NOT_ANSWERED:
                    payload["review"] = "failed"
                elif reply == PENDING_REPLY:
                    # Only a job handle: Codex has not answered, so nothing is done yet.
                    payload["review"] = "pending"
                else:
                    if payload["gate"] == "blocked":
                        _gate_event(cwd, session, "review_done", len(payload["files"]))
                    payload.update(files=[], edits_since=0, reminders=0, sensitive=False,
                                   gate="none", review="done", skip_logged=0)
            elif reply == PENDING_REPLY:
                # Asked, not answered yet: the gate does not ask for it again.
                payload["consult_pending"] = True
            else:
                # A consultation: it counts for this change once Codex answered.
                failed = reply == NOT_ANSWERED
                _gate_event(cwd, session, "consult_failed" if failed else "consult_done",
                            len(payload["files"]))
                if not failed:
                    payload["consulted"] = True
            _save(record, payload)
            return 0
        elif tool in RUN_TOOLS:
            if not record.exists():
                return 0  # Nothing changed yet: a command alone keeps nothing.
            payload = _load(record)
            remind = (len(payload["files"]) >= THRESHOLD and payload["edits_since"] > 0
                      and payload["reminders"] < MAX_REMINDERS)
            if not remind:
                return 0
            if clock() < deadline:
                chosen = (mode or _codex_allowed_mode)(cwd)
                if chosen in ("decide", "offer"):
                    output = _context("PostToolUse", (DECIDE if chosen == "decide" else OFFER)
                                      .format(count=len(payload["files"])))
            payload["reminders"] += 1
            payload["edits_since"] = 0
            _save(record, payload)
        else:
            relative = _relative(event)
            if relative is None:
                return 0
            payload = _load(record)
            digest = _digest(relative.casefold())
            if _new_set(payload, digest):
                payload.update(_CHANGE_RESET, files=[])
            if digest not in payload["files"] and len(payload["files"]) < MAX_DIGESTS:
                payload["files"].append(digest)
            payload["edits_since"] += 1
            if SENSITIVE.search(relative):
                payload["sensitive"] = True
            if payload["review"] == "done":
                # Edits after a review, before Claude stops, are the fixes for its
                # findings: not a new change set to review again.
                payload["review"] = "fixups"
            _save(record, payload)
        written = False
        if output is not None and clock() < deadline:
            try:
                stdout.write(output)
                written = True
            except Exception:
                written = False
        if written and shown_event is not None:
            _gate_event(cwd, session, *shown_event)
        elif not written and unused is not None:
            # Nobody saw the gate's message (out of time, or the write failed):
            # the gate was not used, so the state goes back to what it was.
            _save(record, unused)
        _prune(folder, now())
    except Exception:
        pass  # No diagnostic text, state marker or user-facing message.
    return 0
