# Network

This document says which network connections ModelQuad makes, which ones the
programs it starts make, and how to check.

## The short version

- ModelQuad's own processes connect only to loopback addresses. An audit hook
  refuses any other connection from them. There is no ModelQuad server, no
  update check and no telemetry.
- ModelQuad talks to Claude Code over standard input and output, and to its
  own optional local service over loopback (`127.0.0.1`). `modelquad settings
  open` serves a one-time settings page on loopback too, while you use it.
- The Codex CLI, which ModelQuad starts, connects to OpenAI under your own
  Codex login. Claude Code, which starts ModelQuad, connects to Anthropic.
  ModelQuad does not proxy, intercept or observe that traffic.

## The loopback rule

`src/modelquad/netpolicy.py` adds an audit hook (`sys.addaudithook`) in
each ModelQuad entry point: the `modelquad` command (which also runs the
hooks and the local service), the MCP server (`modelquad-mcp`) and the
background job runner. The hook refuses `socket.connect` and `socket.sendto`
to any address that is not loopback, before the call is made. `localhost`,
loopback IP addresses (`127.0.0.0/8`, `::1`, and IPv4-mapped loopback) and
local socket or pipe paths are allowed; any other address or host name is
refused (`tests/test_netpolicy.py`).

The hook covers ModelQuad's own Python processes only. Programs ModelQuad
starts, such as the Codex CLI, are separate processes; the hook does not see
or restrict them.

A provider that would be called directly from ModelQuad's own process cannot
be chosen to run work. None ships, and the loopback rule would refuse its
connection.

Check the rule with:

    modelquad network
    modelquad network --json

`modelquad privacy` shows the same rule.

## Programs ModelQuad starts

These programs make their own connections, with their own configuration and
your own login. ModelQuad gives them no credential of its own and does not see
their traffic.

- **Codex worker** (`codex exec`). Started for a Codex call that you approved
  in Claude Code. It receives the task and the text of the files the request
  names on its standard input, and sends them to OpenAI (see
  [PRIVACY.md](PRIVACY.md)). It runs in an empty temporary folder, read-only,
  with web search and its command, plugin, app and browser tools turned off.
  The run carries Codex's own client identity; ModelQuad adds none.
- **Codex quota read** (`codex app-server`, `account/rateLimits/read`). Codex
  asks OpenAI for your rate limits. No prompt is sent and no model turn runs.
  This is the one Codex call in which ModelQuad names itself, as `modelquad`
  with its version.
  - The MCP server does this once when it starts and then every 240 seconds
    (about every 4 minutes) while it runs, once the notice of what ModelQuad
    sends where has been shown (a plugin-only install reads nothing before
    its first Codex call). Claude Code sessions share one
    reading: a session skips its read when another one read less than 230
    seconds earlier.
  - `modelquad usage-refresh` and the `modelquad_usage_refresh` tool do it on
    demand, at most once a minute unless forced.
  - To turn off the background reads, set the environment variable
    `MODELQUAD_CODEX_QUOTA_REFRESH` to `0` where Claude Code starts ModelQuad,
    for example with `setx MODELQUAD_CODEX_QUOTA_REFRESH 0`, then restart
    Claude Code. On-demand reads still work.
  - Starting a Codex task does not start a quota read and does not wait for
    one.
- **Version and feature checks**: `codex --version` and `codex features list`,
  to learn which Codex is installed and to check the features ModelQuad turns
  off. Whether Codex contacts OpenAI for these is up to Codex.

ModelQuad does not start Claude. Claude Code starts ModelQuad.

`modelquad tools` shows install commands for missing tools; it does not run
them.

## Local connections

- **MCP server** (`modelquad-mcp`): Claude Code starts it and talks to it over
  standard input and output. It opens no listening socket.
- **Local service** (optional; `modelquad serve`, or in the background after
  `modelquad service install` or `modelquad service start`): an HTTP API on
  `127.0.0.1`, port `8765` by default. It binds to the loopback address only
  and takes no bind address.
  - It answers only requests whose `Host` header names loopback (`127.0.0.1`,
    `localhost` or `::1`), which blocks DNS rebinding.
  - It refuses requests from another browser origin, and requests marked
    `Sec-Fetch-Site: cross-site`.
  - Every request except `POST /api/listener-proof` must carry the
    installation secret from `<state folder>\service-secret`, which only your
    user and SYSTEM can read. The listener proof lets ModelQuad's own clients
    check that the listener is this installation's service before they send
    the secret.
  - It publishes no API documentation pages.
  - Loopback is reachable by every account on the computer. See
    [SECURITY.md](SECURITY.md) for what the secret does and does not protect.
  - Every minute while it runs, it reads the local quota files and runs the
    doctor checks. It does not start a Codex quota read.
- **Settings page** (`modelquad settings open`): a one-time page for your
  browser, served on `127.0.0.1` on a port the system picks. It makes no
  outgoing connection.
  - The page's address carries a random secret made for this run, and every
    request must carry it.
  - It answers only requests that name this server exactly in `Host` and,
    when the browser sends one, in `Origin`.
  - It closes when you press Done, or after 15 minutes without a request.
- **Other local clients**: the startup reminder hook, the MCP server (for
  background jobs and waiting answers) and some CLI commands (for example
  `modelquad settings job-runner`) reach the local service over loopback.

## Provider traffic is the provider's

What Codex and Claude Code send, and to which hosts, is decided by those
programs and governed by OpenAI's and Anthropic's terms. ModelQuad does not
claim that delegated work can run offline: Codex needs its own connection to
OpenAI to do anything.
