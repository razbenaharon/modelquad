# Network

This document says which network connections ModelQuad makes, which ones the
programs it starts make, and how to check.

## The short version

- ModelQuad's own processes connect only to loopback addresses. An audit hook
  refuses a `socket.connect` or `socket.sendto` made through Python's socket
  module to any other address (see [The loopback rule](#the-loopback-rule)
  for what it does not cover). There is no ModelQuad server, no update check
  and no telemetry.
- ModelQuad talks to Claude Code over standard input and output. It opens no
  listening socket and runs no local HTTP service.
- The Codex CLI, which ModelQuad starts, connects to OpenAI under your own
  Codex login. Claude Code, which starts ModelQuad, connects to Anthropic.
  ModelQuad does not proxy, intercept or observe that traffic.

## The loopback rule

`src/modelquad/netpolicy.py` adds an audit hook (`sys.addaudithook`) in
each ModelQuad entry point: the `modelquad` command (which also runs the
hook) and the MCP server (`modelquad-mcp`). The hook refuses
`socket.connect` and `socket.sendto` to any address that is not loopback,
before the call is made. `localhost`, loopback IP addresses (`127.0.0.0/8`,
`::1`, and IPv4-mapped loopback) and local socket or pipe paths are allowed;
any other address or host name is refused (`tests/test_netpolicy.py`).

The hook sees only the calls that raise those audit events, which are the
calls made through Python's `socket` module. Two kinds of call raise
neither and are not refused: a connect made by Windows asyncio's default
(Proactor) event loop, and a host name lookup (`socket.getaddrinfo`), which
goes to the system resolver. ModelQuad's own code makes neither: it opens
no client connection at all. The hook is a check on that code, not a sandbox
for anything else that runs in the process.

The hook covers ModelQuad's own Python processes only. Programs ModelQuad
starts, such as the Codex CLI and Git, are separate processes; the hook does
not see or restrict them.

`modelquad privacy` (or `modelquad privacy --json`) shows the rule.

## Programs ModelQuad starts

These programs make their own connections, with their own configuration and
your own login. ModelQuad gives them no credential of its own and does not see
their traffic.

- **Codex worker** (`codex exec`). Started for a Codex call that you approved
  in Claude Code, or that your standing grant allowed. It receives the task
  and the text of the files the request names on its standard input, and
  sends them to OpenAI (see [PRIVACY.md](PRIVACY.md)). It runs in an empty
  temporary folder, read-only, with web search, agents and its command,
  plugin, app and browser tools turned off. The run carries Codex's own
  client identity; ModelQuad adds none.
- **Version and feature checks**: `codex --version` and `codex features list`,
  before a Codex call and in `modelquad doctor`, to learn which Codex is
  installed and to check the features ModelQuad turns off. Whether Codex
  contacts OpenAI for these is up to Codex.
- **Git**, when it is installed: `git check-ignore` for the files a request
  names, `git rev-parse --show-toplevel` to find a repository's top folder,
  and, in a development checkout of ModelQuad, `git rev-parse --verify HEAD`
  for the build shown by `modelquad --version`. These read the local
  repository only; ModelQuad never asks Git to fetch or push.

ModelQuad does not start Claude. Claude Code starts ModelQuad.

Earlier builds also asked `codex app-server` for your Codex quota and ran an
optional local HTTP service on loopback. This build does neither.

## Local connections

- **MCP server** (`modelquad-mcp`): Claude Code starts it and talks to it over
  standard input and output. It opens no listening socket.
- **Hook** (`modelquad hook-review claude`, plugin only): Claude Code starts
  it for each hook event and passes the event on standard input. It makes no
  connection.

## Provider traffic is the provider's

What Codex and Claude Code send, and to which hosts, is decided by those
programs and governed by OpenAI's and Anthropic's terms. ModelQuad does not
claim that a review can run offline: Codex needs its own connection to
OpenAI to do anything.
