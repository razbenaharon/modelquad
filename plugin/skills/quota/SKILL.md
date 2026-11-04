---
name: quota
description: Show Claude's and Codex's quota, reset times and this repository's Codex setting. Display only; starts nothing.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# Quotas

Call `modelquad_status` with this session's absolute project directory as
`project_directory`. Show the user the first lines of its reply as written:

- Claude's and Codex's 5-hour and weekly quota left, and when the 5-hour
  window resets in the user's local time. "(as of 14:02)" marks an old
  reading. A reading ModelQuad could not get is unknown; never guess a number.
- The account kinds, and this repository's Codex setting.
- If a reset is close, say so.

This is information only. Do not start any work because of these numbers;
the user decides.
