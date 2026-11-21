# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one review flow: a request, its context package, one executor, a result.

    ReviewRequest -> package() -> ReviewExecutor.execute() -> reply

``ReviewRequest`` is what Claude asked for. ``package()`` says exactly what
would be sent (the manifest) without starting anything. ``run()`` checks the
user's consent and the repository's rules, hands the request to the one
executor (the Codex CLI, ``execution.codex.CodexExecutor``) and returns a
reply whose only free text is Codex's answer.

Nothing here selects files, starts background work or chooses a provider.
A second backend would implement ``ReviewExecutor``; none exists.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .execution.base import DEFAULT_TIMEOUT_SECONDS, ExecutionRequest, ExecutionResult

#: The most file paths one request names.
MAX_NAMED_FILES = 32
#: The most answer text a reply carries inline.
MAX_SHOWN_OUTPUT_CHARS = 20_000
#: A review may be followed by this many completion rounds (files Codex said it needed).
MAX_COMPLETION_ROUNDS = 2
MANIFEST_SCHEMA = 1

WORKER = "worker_refused"
DUPLICATE = "duplicate_in_flight"
PACKAGE_CHANGED = "package_changed"
ROUNDS_EXHAUSTED = "completion_rounds_exhausted"
FOLLOWS_UNKNOWN = "follows_unknown"
FILES_WITHHELD = "named_files_withheld"
NOTICE_FIRST = "notice_first"

FILES_WITHHELD_NEXT = (
    "Call again without them. Never paste an excluded file's contents into the task text or "
    "another file: the exclusion is the user's. To show Codex a change to files that are "
    "sent, put that diff in the task text.")
FILES_UNREADABLE_NEXT = (
    "No file can be sent until that is fixed: tell the user. A call without `files` "
    "sends only the task text.")
NOTICE_CHANGED_NEXT = (
    "Codex was not started. ModelQuad's notice changed since the user last saw it: show "
    "them the notice above in full, as written, and say that it changed. If the user "
    "wants to go ahead, call the same tool again; Claude Code asks them again.")
NOTICE_FIRST_NEXT = (
    "Codex was not started. Show the user the notice above in full, as written: it is "
    "what ModelQuad sends where, and it is shown once, before the first Codex call. If "
    "the user wants to go ahead, call the same tool again; Claude Code asks them again.")
FINDINGS_NOTE = (
    "For each finding, check the claim and its reasoning against the code and the user's "
    "requirements before changing anything; Codex did not see the conversation and can be "
    "wrong, and neither its confidence nor your agreement is proof. Record your verdict and "
    "its evidence with review_verdict (this decision_id; action to_fix for one you "
    "accept), then fix, then record it again with action fixed and fix_verified.")
COMPLETION_NOTE = (
    "Codex said these files would have helped and they were not sent. Nothing is added by "
    "itself. To send them, show the user the larger package (review_package with "
    "these files added) and, if they agree, call delegate again with the files and "
    "follows set to this decision_id.")
OUTPUT_CUT_MARKER = (
    "\n\n[ModelQuad cut this answer here: the first {shown} of {whole} characters are shown. "
    "Ask Codex for a shorter answer, or split the request.]")
REFUSAL_FALLBACK = "ModelQuad's rules did not allow this Codex call."
NOT_RECORDED_NOTE = (
    "ModelQuad could not write this call to its review ledger (the file was locked or could "
    "not be written), so the review report misses it or shows it with no end recorded, and "
    "its findings cannot take a verdict. Tell the user.")
PACKAGE_NOT_RECORDED_NOTE = (
    "ModelQuad could not write what this call sent to its review ledger (the file was locked "
    "or could not be written), so the review report misses its package and a completion "
    "round cannot follow it. Tell the user.")
WORKER_REPORTED = "worker_reported"

#: One plain sentence per closed reason; a person reads the sentence, never the code.
_LINES: dict[str, str] = {
    WORKER: "This session was started by ModelQuad for Codex, so it cannot ask Codex again.",
    DUPLICATE: ("The same request is already running, in this Claude Code session or another "
                "one, so ModelQuad started nothing new."),
    "task_type_not_read_only": ("Codex through ModelQuad takes only read-only review, exploration "
                                "or architecture tasks."),
    PACKAGE_CHANGED: ("The files changed since the package was shown, or the request names "
                      "different files, so nothing was sent. Show the package again "
                      "(review_package) and ask again with its package_id."),
    ROUNDS_EXHAUSTED: (f"This review already had {MAX_COMPLETION_ROUNDS} completion rounds, so "
                       "ModelQuad started nothing. Report what Codex could not see as a limit "
                       "of the review, or start a new review."),
    FOLLOWS_UNKNOWN: ("follows names a review ModelQuad has no record of in this folder, so "
                      "nothing was started. Leave follows out, or pass the decision_id of the "
                      "reply that listed the missing files."),
    "working_directory_missing": ("That working folder does not exist, so nothing was started. "
                                  "Pass the absolute path of an existing project folder."),
    "working_directory_not_a_folder": ("That working_directory is a file, not a folder, so nothing "
                                       "was started. Pass the project folder and name the file in files."),
    "model_not_valid": ("That model name is not a valid Codex model id, so nothing was started. "
                        "Leave model out to use Codex's default, or pass a model id Codex lists."),
    "reasoning_effort_not_valid": ("That reasoning_effort is not a value ModelQuad passes to Codex, so "
                                   "nothing was started. Leave it out to use Codex's default."),
    "prompt_empty": "The task text (prompt) is empty, so nothing was started. Say what Codex should do.",
    "files_not_valid": (f"files must be a list of at most {MAX_NAMED_FILES} paths relative to "
                        "working_directory, so nothing was started."),
    "invalid_request": ("The request could not be used (the working folder, the model or another "
                        "argument is not valid), so nothing was started."),
    "cli_not_found": "The Codex CLI was not found on this computer. Run: modelquad doctor",
    "timed_out": "Codex did not return in time.",
    WORKER_REPORTED: ("Codex ended with an error or was stopped, and gave no answer. ModelQuad "
                      "does not pass Codex's error text on. If a model was named, check the name; "
                      "otherwise check that Codex is signed in and has usage left, or run: "
                      "modelquad doctor"),
    "stop_requested": "The call was cancelled, and ModelQuad stopped Codex.",
}


@dataclass(frozen=True)
class ReviewRequest:
    """What Claude asked for. ``files`` are relative to ``working_directory``."""

    task_type: str
    prompt: str
    working_directory: str
    files: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    model: str | None = None
    reasoning_effort: str | None = None
    trigger: str = "claude"
    #: The package the user was shown (``package()``); a different one is not sent.
    package_id: str | None = None
    #: The earlier review this one completes with files it said it needed.
    follows: str | None = None


class ReviewExecutor(Protocol):
    """Runs one read-only review. The Codex CLI is the only implementation."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


@dataclass(frozen=True)
class Session:
    """What this server process knows about the Claude Code session it serves."""

    #: The session started without Claude Code's per-call prompt (a standing grant).
    standing: bool = False
    #: The folder the session started in; calls without the prompt stay inside it.
    project: str = ""
    #: This process runs inside a Codex worker (``mcp.worker_guard``).
    worker: bool = False


def refusal(reason: str, message: str | None = None, **extra: Any) -> dict:
    """The reply for a call that started nothing."""
    payload = {"delegated": False, "executed": False, "refused": True, "started": False,
               "reason": reason, **extra}
    # The sentence travels in the data: Claude Code hands Claude the data of a
    # reply, not the lines written for a person.
    message = message or refusal_line({"reason": reason})
    if message != REFUSAL_FALLBACK:
        payload["message"] = message
    return payload


def refusal_line(payload: Any) -> str:
    """The one sentence a person reads about why Codex was not started; never the code."""
    if not isinstance(payload, dict):
        return REFUSAL_FALLBACK
    message = payload.get("message")
    reason = payload.get("reason")
    if isinstance(message, str) and message.strip() and reason != message:
        return message
    if isinstance(reason, str):
        if reason in _LINES:
            return _LINES[reason]
        from .execution import noshell

        if reason in noshell.REFUSAL_MESSAGES:
            return noshell.REFUSAL_MESSAGES[reason]
    return REFUSAL_FALLBACK


# ----------------------------------------------------------------- the gates


def _input_refusal(request: ReviewRequest) -> dict | None:
    """A request that cannot be used at all. The folder is already known to be local."""
    from .execution.closed import CODEX_REASONING_EFFORTS, safe_model_id

    reason = None
    try:
        folder = Path(request.working_directory)
        if not folder.exists():
            reason = "working_directory_missing"
        elif not folder.is_dir():
            reason = "working_directory_not_a_folder"
    except (OSError, ValueError, TypeError):
        reason = "working_directory_missing"
    if reason is None and (not isinstance(request.prompt, str) or not request.prompt.strip()):
        reason = "prompt_empty"
    if reason is None and request.model is not None and safe_model_id(request.model) != request.model:
        reason = "model_not_valid"
    if (reason is None and request.reasoning_effort is not None
            and request.reasoning_effort not in CODEX_REASONING_EFFORTS):
        reason = "reasoning_effort_not_valid"
    return None if reason is None else refusal(reason)


def _files_refusal(files: Any) -> dict | None:
    if (type(files) is not tuple or len(files) > MAX_NAMED_FILES
            or any(type(name) is not str for name in files)):
        return refusal("files_not_valid")
    return None


def _withheld_refusal(working_directory: str, files: tuple[str, ...]) -> dict | None:
    """A refusal, before Codex starts, when a named file would not be sent.

    Without it Codex would run, and spend the user's time and quota, on a
    review missing a file Claude meant it to read.
    """
    from .execution import file_rules, noshell

    if not files:
        return None
    _allowed, withheld, _invalid = screen_named(working_directory, files)
    if not withheld:
        return None
    reasons = noshell._POLICY_GUIDANCE
    unreadable = [reason for _, reason in withheld
                  if reason in (file_rules.SETTINGS_UNREADABLE, file_rules.IGNORE_FILE_UNREADABLE,
                                file_rules.GIT_UNANSWERED)]
    if unreadable:
        message = reasons[unreadable[0]] + " " + FILES_UNREADABLE_NEXT
    else:
        named = [f"{name}: {reasons.get(reason, 'it was excluded.')}" for name, reason in withheld[:8]]
        message = ("These named files would not be sent to Codex. " + " ".join(named) + " "
                   + FILES_WITHHELD_NEXT)
    return refusal(FILES_WITHHELD, message,
                   skipped_files=[f"{name} ({reason})" for name, reason in withheld])


def screen_named(working_directory: str, files) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """(allowed, withheld with a closed reason, invalid) for the named files.

    The same read rules as the run (Claude Code Read deny rules, Git,
    ``.modelquadignore``); reads no file's contents.
    """
    from .execution import file_rules, noshell

    names = noshell.explicit_names(list(files), working_directory)
    valid, invalid = [], []
    for name in dict.fromkeys(names):
        try:
            valid.append(noshell.canonical_path(name))
        except noshell.TargetError:
            invalid.append(name)
    allowed, withheld = file_rules.screen(working_directory, valid)
    return allowed, withheld, invalid


def notice() -> dict | None:
    """The data-flow notice in place of the first Codex call, or None once it was shown."""
    from .disclosure import DATA_FLOW, notice_shown, record_notice_shown, shown_version

    if notice_shown():
        return None
    seen = shown_version()  # an earlier version, or 0 for never
    payload: dict[str, Any] = {"status": NOTICE_FIRST, "codex_started": False,
                               "notice": list(DATA_FLOW),
                               "next": NOTICE_CHANGED_NEXT if seen else NOTICE_FIRST_NEXT}
    if seen:
        payload["notice_changed"] = True
    try:
        record_notice_shown("first_call")
    except OSError:
        payload["note"] = "ModelQuad could not record that the notice was shown, so it shows it again."
    return payload


def gate(request: ReviewRequest, session: Session, *, starts_codex: bool = True,
         policy_out: list | None = None) -> dict | None:
    """Every reason not to go on, in order; None to go on.

    ``policy_out``, when given, receives the repository policy this gate read,
    so the caller packs under the same caps without reading the file again.

    ``starts_codex`` is False for a package preview: the consent gates that
    protect a Codex start (the standing grant, the first-use notice) do not
    apply to showing what would be sent, while everything that decides
    whether a folder or a file may be read at all still does.
    """
    from . import grant, localpath, repo_policy, switch

    if session.worker:
        # A worker gets its own refusal first (invariant 1); nothing opens its folder.
        return refusal(WORKER)
    files_refusal = _files_refusal(request.files)
    if files_refusal is not None:
        return files_refusal
    # Before any file-system call: a network share would be opened by the first stat.
    if localpath.reason(request.working_directory) is not None:
        return refusal(localpath.UNUSABLE, repo_policy.UNUSABLE_MESSAGE)
    off = switch.refusal()
    if off is not None:
        return refusal(off["reason"], off["message"])
    if starts_codex and session.standing:
        # Registered without Claude Code's per-call prompt: a folder without a
        # grant (or a grant turned off since) gets nothing, by no other route,
        # and the model cannot point a call at a folder the session did not start in.
        if not grant.allows(request.working_directory):
            return refusal(grant.NO_GRANT, grant.NO_GRANT_MESSAGE)
        if not grant.inside_session_project(request.working_directory, session.project):
            return refusal(grant.OTHER_FOLDER, grant.OTHER_FOLDER_MESSAGE)
    unusable = _input_refusal(request)
    if unusable is not None:
        return unusable
    if request.task_type not in repo_policy.TASK_KINDS:
        return refusal("task_type_not_read_only")
    repo_refusal, policy = repo_policy.gate(request.working_directory, request.task_type,
                                            files=request.files)
    if policy_out is not None and policy is not None:
        policy_out.append(policy)
    if repo_refusal is not None:
        return refusal(repo_refusal["reason"], repo_refusal["message"])
    if starts_codex:
        first = notice()
        if first is not None:
            return first
        return _withheld_refusal(request.working_directory, request.files)
    return None


# --------------------------------------------------------------- the package


def _caps(working_directory: str) -> dict:
    from . import repo_policy

    return repo_policy.load(working_directory).caps


def _package_id(task_type: str, prompt_digest: str, sent: list[dict]) -> str:
    body = json.dumps({"schema": MANIFEST_SCHEMA, "task_type": task_type, "prompt": prompt_digest,
                       "files": [[item["path"], item["bytes"], item["sha256"]] for item in sent]},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def manifest(task_type: str, prompt: str, sent: list[dict], skipped: list[dict]) -> dict:
    """The compact record of one package: what is sent, what is left out, and its id.

    Metadata only: relative paths, sizes and digest prefixes of the text as
    sent (after credential redaction), never contents. The id covers the
    task kind, the task text and every sent file, so the same id means the
    same package. It is a digest, not a signature.
    """
    from .execution import noshell

    data = prompt.encode("utf-8")
    prompt_digest = hashlib.sha256(data).hexdigest()[:16]
    total = sum(item["bytes"] for item in sent)
    return {
        "schema": MANIFEST_SCHEMA,
        "package_id": _package_id(task_type, prompt_digest, sent),
        "task_type": task_type,
        "prompt": {"bytes": len(data), "sha256": prompt_digest},
        "files": [dict(item) for item in sent],
        "skipped": [dict(item) for item in skipped],
        "file_count": len(sent),
        "total_bytes": total,
        "limits": {"max_files": noshell.MAX_FILES, "max_file_bytes": noshell.MAX_FILE_BYTES,
                   "max_total_bytes": noshell.MAX_TOTAL_BYTES,
                   "max_prompt_bytes": noshell.MAX_PROMPT_BYTES},
    }


def package(request: ReviewRequest, caps: dict | None = None) -> dict:
    """What a run of this request would send now. Starts nothing.

    Packs the files exactly as the run does (the read rules, the secret
    screen, the caps) and returns the manifest, or the closed refusal the run
    would give. Reads the files; sends nothing anywhere.
    """
    from .execution import noshell

    caps = caps if caps is not None else _caps(request.working_directory)
    report: dict = {}
    try:
        noshell.supplied_prompt(request.prompt, request.working_directory, files=list(request.files),
                                completion_protocol=True, report=report, caps=caps)
    except noshell.SuppliedContextRefused as refused:
        diagnostics = getattr(refused, "host_diagnostics", ()) or ()
        skipped = [{"path": item["path"], "reason": item["reason"]}
                   for item in (entry.to_host_dict() for entry in diagnostics)
                   if item["reason"] in noshell.AUDIT_REASONS][:noshell.MAX_SKIPPED]
        if refused.reason == "supplied_file_secret" and request.files:
            return _secret_refusal(request, caps, skipped, refused.secret_kinds)
        return refusal(refused.reason, skipped_files=skipped)
    result = manifest(request.task_type, request.prompt, report.get("sent", []),
                      [item for item in report.get("skipped", [])
                       if item.get("reason") in noshell.AUDIT_REASONS])
    lowered = {key: value for key, value in caps.items()
               if key in ("max_files", "max_total_bytes") and type(value) is int
               and value < result["limits"][key]}
    if lowered:
        result["limits"].update(lowered)
        result["limits"]["lowered_by"] = ".modelquad.toml"
    return result


#: Why a named file was left out of the retry package (``left_out`` in a refusal).
SECRET_NAME = "secret_file_name"        # named like a secrets file; never opened
SECRET_CONTENT = "secret_file_content"  # its text holds a recognisable credential
SECRET_PATH = "secret_path"             # the path itself looks like a credential

_LEFT_OUT_WHY = {
    SECRET_NAME: "its name looks like a secrets file (an environment or credential file), so "
                 "ModelQuad did not open it; its contents were not checked",
    SECRET_CONTENT: "its text contains what looks like a real credential (a private key or a "
                    "recognisable access token)",
    SECRET_PATH: "the path itself looks like a credential",
}
_LEFT_OUT_SHORT = {SECRET_NAME: "its name", SECRET_CONTENT: "its contents", SECRET_PATH: "its path"}
RETRY_NEXT = ("Nothing was sent to Codex. ModelQuad does not repeat the path of a file like this. "
              "The package below leaves it out: tell the user which file it is (by its position in "
              "your files list) and why, show the files that would be sent, and ask whether the "
              "review is still useful without it. Only if they agree, call again with exactly those "
              "files and its package_id. If Codex needs what the file holds, supply a redacted copy "
              "instead.")


def _secret_refusal(request: ReviewRequest, caps: dict | None, skipped: list[dict],
                    kinds: dict[str, str]) -> dict:
    """A named file stopped the package: say which and why, and offer the package without it.

    The reason stays ``supplied_file_secret``. The path of such a file is never
    echoed: the packer's privacy screen reports it as ``<path>``
    (``noshell._required_names``), and this keeps that. ``left_out`` gives instead
    the file's 1-based position in the request's ``files`` (Claude's own list)
    and whether its name, its contents or its path stopped it.
    ``kinds`` is what the packing pass that refused recorded for each file it
    stopped; this reads no file to explain it. A file the read rules withheld
    was never packed, so it is not in ``kinds`` and is never opened here.
    ``retry_package`` is the manifest of the same request without those files.
    Starts nothing and sends nothing: a retry is a new call, sent only with that
    package_id, which the user agrees to.
    """
    from dataclasses import replace

    from .execution import noshell

    by_kind = {"path": SECRET_PATH, "name": SECRET_NAME, "content": SECRET_CONTENT}
    left_out: list[dict] = []
    keep: list[str] = []
    for position, entry in enumerate(request.files, start=1):
        name = noshell.relevant_entry(entry, request.working_directory) if isinstance(entry, str) else ""
        try:
            name = noshell.canonical_path(name)
        except noshell.TargetError:
            keep.append(entry)
            continue
        reason = by_kind.get(kinds.get(name.casefold(), ""))
        if reason is None:
            keep.append(entry)
            continue
        left_out.append({"position": position, "path": "<path>", "reason": reason})
    if not left_out:
        return refusal("supplied_file_secret", skipped_files=skipped)
    reasons = {item["reason"] for item in left_out}
    named = "; ".join(f"The file listed as number {item['position']} in files: "
                      f"{_LEFT_OUT_WHY[item['reason']]}" for item in left_out)
    if reasons == {SECRET_NAME}:
        first = "ModelQuad stopped this review because of a file name, not its contents. "
    else:
        first = "ModelQuad stopped this review because a named file looks like it holds a secret. "
    message = first + named + ". "
    retry = package(replace(request, files=tuple(keep), package_id=None), caps)
    extra: dict[str, Any] = {"skipped_files": skipped, "left_out": left_out}
    if retry.get("refused"):
        message += "A package without those files cannot be sent either: " + refusal_line(retry)
    else:
        extra["retry_package"] = retry
        message += RETRY_NEXT
        if not retry["files"]:
            message += " Without them Codex would get only the task text."
    return refusal("supplied_file_secret", message, **extra)


def package_lines(result: dict) -> list[str]:
    """The package, for a person: what Codex would get, its size, and what is left out."""
    if result.get("refused"):
        lines = ["This package cannot be sent.", refusal_line(result)]
        return lines + retry_lines(result)
    files = result["files"]
    if not files:
        lines = [f"Codex would get only the task text ({result['prompt']['bytes']} bytes)."]
    else:
        lines = [f"Codex would get the task text ({result['prompt']['bytes']} bytes) and "
                 f"{len(files)} file(s), {result['total_bytes']} bytes, named in this request:"]
        lines += [f"- {item['path']} ({item['bytes']} bytes)" for item in files]
    if result["skipped"]:
        lines.append("Left out: " + ", ".join(
            f"{item['path']} ({item['reason']})" for item in result["skipped"]))
    limits = result["limits"]
    lines.append(f"Limits: {limits['max_files']} files, {limits['max_total_bytes']} bytes in all"
                 + (" (lowered by this repository's .modelquad.toml)." if "lowered_by" in limits else "."))
    lines.append("Sizes are bytes as sent, after credentials are redacted. Token counts are "
                 "Codex's own and are known only after a run; ModelQuad does not estimate them "
                 "or a price.")
    return lines


def retry_lines(result: dict) -> list[str]:
    """For a refusal that offers one: what was left out, and the package without it."""
    retry = result.get("retry_package")
    if not isinstance(retry, dict):
        return []
    lines = ["Left out: " + ", ".join(
        f"file {item['position']} in the list (stopped by {_LEFT_OUT_SHORT[item['reason']]})"
        for item in result.get("left_out", []))]
    lines.append(f"Without it, the package is {retry['package_id']}:")
    return lines + package_lines(retry)


# ------------------------------------------------------------------- the run


_running: set[str] = set()
_running_lock = threading.Lock()
RUN_LOCK_FOLDER = "running"


def _hold_package(key: str):
    """Hold the package's run lock across server processes, or None if another holds it.

    Each Claude Code session has its own server process, so the in-process set
    above cannot see a run started by another session. This takes an OS lock
    on one byte of ``<state root>/running/<package id>.lock``. The operating
    system releases the lock when the handle closes or the process ends, so a
    server that crashes or is killed leaves no lock behind; the file itself is
    only a name to lock. Returns a callable that releases the lock, ``None``
    when another process holds it, or a no-op release when the lock file
    cannot be opened (the in-process rule still applies; the docs say the
    cross-session rule needs a shared, writable state folder).
    """
    import os

    from .paths import state_root

    try:
        folder = state_root() / RUN_LOCK_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{key}.lock"
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return lambda: None
    try:
        _lock_byte(handle)
    except OSError:
        os.close(handle)
        return None

    def release() -> None:
        try:
            _lock_byte(handle, unlock=True)
        except OSError:
            pass
        os.close(handle)
        try:
            # Fails harmlessly while another process has the file open.
            os.unlink(path)
        except OSError:
            pass

    return release


def _lock_byte(handle: int, *, unlock: bool = False) -> None:
    """Lock (without waiting) or unlock the first byte; raises OSError when it is held."""
    import os

    try:
        import msvcrt
    except ImportError:  # not Windows: only the suite on another system gets here
        import fcntl

        fcntl.lockf(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
        return
    os.lseek(handle, 0, os.SEEK_SET)  # msvcrt locks from the current position
    msvcrt.locking(handle, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)


def _round(request: ReviewRequest) -> tuple[int, dict | None]:
    """The completion round this request is (0: a first review), or a refusal."""
    if request.follows is None:
        return 0, None
    from . import review_ledger

    try:
        earlier = review_ledger.package_round(request.follows, request.working_directory)
    except Exception:  # noqa: BLE001 -- an unreadable ledger cannot vouch for the chain
        earlier = None
    if earlier is None:
        return 0, refusal(FOLLOWS_UNKNOWN)
    if earlier >= MAX_COMPLETION_ROUNDS:
        return 0, refusal(ROUNDS_EXHAUSTED)
    return earlier + 1, None


def _cap_output(payload: dict) -> None:
    output = payload.get("output")
    if not isinstance(output, str) or len(output) <= MAX_SHOWN_OUTPUT_CHARS:
        return
    payload["output"] = output[:MAX_SHOWN_OUTPUT_CHARS] + OUTPUT_CUT_MARKER.format(
        shown=MAX_SHOWN_OUTPUT_CHARS, whole=len(output))
    payload["output_truncated"] = True
    payload["output_chars"] = len(output)


def _usage(metadata: dict, seconds: float | None) -> dict:
    """What the run used, each in its own unit; an absent count is unknown, not zero."""
    tokens = metadata.get("worker_tokens") if isinstance(metadata.get("worker_tokens"), dict) else {}
    return {"model_calls": 1 if metadata.get("worker_started") else 0,
            "input_tokens": tokens.get("input_tokens"),
            "cached_input_tokens": tokens.get("cached_input_tokens"),
            "output_tokens": tokens.get("output_tokens"),
            "reasoning_output_tokens": tokens.get("reasoning_output_tokens"),
            "seconds": seconds,
            "tokens_source": "codex" if tokens else None}


def run(request: ReviewRequest, executor: ReviewExecutor, session: Session = Session(), *,
        on_spawn=None, on_packed=None, stop_requested=None, clock=None) -> dict:
    """One review, start to finish. Returns the reply's data; never raises for a refusal."""
    import time

    from . import procutil, review_ledger
    from .execution import noshell
    from .execution.base import POLICY_REFUSAL_REASONS, workspace_stability_note
    from .execution.closed import closed_result
    from .models import TaskType

    clock = clock or time.monotonic
    policies: list = []
    refused = gate(request, session, policy_out=policies)
    if refused is not None:
        return refused
    round_number, refused = _round(request)
    if refused is not None:
        return refused
    # What will be sent, by content, read now: the executor compares it with
    # what it packs at launch, so a file that changes in between is not sent.
    shown = package(request, policies[0].caps if policies else None)
    if shown.get("refused"):
        return shown
    if request.package_id is not None and shown["package_id"] != request.package_id:
        return refusal(PACKAGE_CHANGED)
    digests = {item["path"]: item["sha256"] for item in shown["files"]}
    metadata: dict[str, Any] = {"supplied_files": list(request.files), "approval": "delegate",
                                "approved_digests": digests}
    if request.model is not None:
        metadata["model_source"] = "explicit"
    if request.reasoning_effort is not None:
        metadata["reasoning_effort_source"] = "explicit"
    decision_id = uuid.uuid4().hex

    def spawned(pid):
        # Codex exists from here on. The call is recorded now, with what it was
        # given, so a session that closes mid-run still leaves a record; the
        # end of the call replaces both rows.
        try:
            review_ledger.record_start(
                decision_id, working_directory=request.working_directory,
                task_type=request.task_type, trigger=request.trigger)
            review_ledger.record_package(
                decision_id, shown, working_directory=request.working_directory,
                round_number=round_number, follows=request.follows)
        except Exception:  # noqa: BLE001 -- a record that cannot be written does not stop the review
            pass
        if on_spawn is not None:
            on_spawn(pid)

    try:
        execution = ExecutionRequest(
            provider="codex", task_type=TaskType(request.task_type), prompt=request.prompt,
            working_directory=Path(request.working_directory), timeout=request.timeout,
            model=request.model, reasoning_effort=request.reasoning_effort, metadata=metadata,
            on_spawn=spawned, on_packed=on_packed, stop_requested=stop_requested)
    except ValueError:
        return refusal("invalid_request")
    key = shown["package_id"]
    with _running_lock:
        if key in _running:
            # At most one active attempt per request (invariant 2).
            return refusal(DUPLICATE)
        release = _hold_package(key)
        if release is None:
            # Another Claude Code session's server is running this package.
            return refusal(DUPLICATE)
        _running.add(key)
    try:
        started_at = clock()
        # The worker's whole tree is tied to this process: it ends if this server does.
        token = procutil.default_kill_on_close.set(True)
        try:
            result = executor.execute(execution)
        finally:
            procutil.default_kill_on_close.reset(token)
    finally:
        with _running_lock:
            _running.discard(key)
            release()
    seconds = round(clock() - started_at, 1)
    payload = closed_result(result).to_dict()
    payload.pop("session_id", None)
    payload.update(decision_id=decision_id, task_type=request.task_type)
    reason = payload.get("reason")
    started = payload["metadata"].get("worker_started") is True
    if reason in POLICY_REFUSAL_REASONS or reason in noshell.REFUSAL_LABELS:
        payload.update(delegated=False, refused=True, started=started)
        payload["message"] = refusal_line(payload)
    elif (started and reason in _LINES
          and not (isinstance(payload.get("output"), str) and payload["output"].strip())):
        # Why no answer came back, in the data: Claude Code hands Claude the
        # data of a reply, not the lines written for a person.
        payload["message"] = _LINES[reason]
    note = workspace_stability_note(payload["metadata"], request.working_directory)
    if note:
        note.pop("workspace", None)
        payload["worker_note"] = note
    audit = payload["metadata"].get("file_audit")
    if isinstance(audit, dict):
        # The manifest of what the launch actually sent, which the executor packed again.
        payload["package"] = manifest(request.task_type, request.prompt, audit["sent"], audit["skipped"])
        payload["sent_files"] = audit["sent"]
        payload["skipped_files"] = audit["skipped"]
    payload["package_round"] = round_number
    needed = noshell.needed_files(payload.get("output"), request.working_directory,
                                  {item["path"] for item in payload.get("sent_files", ())})
    if needed:
        left = MAX_COMPLETION_ROUNDS - round_number
        payload["completion_request"] = {
            "decision_id": decision_id, "files": needed, "rounds_left": left,
            "note": COMPLETION_NOTE if left else (
                "Codex said these files would have helped. This review has no completion "
                "round left: report them as a limit of what the review covered.")}
    payload["usage"] = _usage(payload["metadata"], seconds)
    try:
        findings = review_ledger.record_run(
            payload, working_directory=request.working_directory, task_type=request.task_type,
            trigger=request.trigger, seconds=seconds)
    except Exception:  # noqa: BLE001 -- the answer matters more than its record
        findings = None
        payload["ledger_note"] = NOT_RECORDED_NOTE
    else:
        try:
            review_ledger.record_package(
                decision_id, payload.get("package"), working_directory=request.working_directory,
                round_number=round_number, follows=request.follows, files_needed=needed,
                usage=payload["usage"])
        except Exception:  # noqa: BLE001 -- the run and its findings are recorded
            payload["ledger_note"] = PACKAGE_NOT_RECORDED_NOTE
    if findings:
        payload["findings"] = [{"finding": item.ref, "severity": item.severity, "file": item.file,
                                "line": item.line, "line_end": item.line_end,
                                "category": item.category, "confidence": item.confidence,
                                "status": "claimed_by_codex"} for item in findings]
        payload["findings_note"] = FINDINGS_NOTE
    _cap_output(payload)
    return payload


def headline(payload: dict) -> str:
    """The first line of a reply: what happened, in the words the review hook reads."""
    if payload.get("status") == NOTICE_FIRST:
        if payload.get("notice_changed"):
            return ("Codex was not started. ModelQuad's notice of what it sends where changed "
                    "since you last saw it:")
        return ("Codex was not started. Before the first Codex call, ModelQuad shows what it "
                "sends where:")
    if payload.get("refused"):
        started = payload.get("started") is True
        return "Codex answer withheld." if started else "Codex was not started."
    output = payload.get("output")
    if isinstance(output, str) and output.strip():
        return "Codex answered."
    if payload.get("metadata", {}).get("timed_out"):
        return "Codex did not return in time."
    return "Codex did not finish: it returned no answer."
