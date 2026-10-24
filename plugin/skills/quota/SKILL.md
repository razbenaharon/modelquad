---
name: quota
description: Show your Claude and Codex usage and reset times. Display only; starts nothing.
disable-model-invocation: true
---

# Quotas

Call `modelquad_status` and report, in plain words:

- Claude's and Codex's 5-hour and weekly usage, as percentages, and when each
  resets in the user's local time.
- How fresh each reading is. A reading ModelQuad could not get is unknown;
  never guess a number.
- If a reset is close, say so.

This is information only. Do not start, move or hand off any work because of
these numbers; the user decides.
