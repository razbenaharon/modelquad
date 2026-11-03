---
name: modelquad-handoff
description: Ask Codex, through ModelQuad, for a read-only second opinion (review, exploration or architecture) on the current task.
argument-hint: "[what Codex should do]"
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# Ask Codex for a second opinion

The user wants Codex's read-only second opinion on this task: $ARGUMENTS

1. Write a short, self-contained task summary: the goal, what is known so
   far, and what answer would help. Codex sees nothing of this conversation.
2. Pick the files Codex needs and pass them in `files` (at most 32). Ask the
   user if you are unsure.
3. Call `modelquad_handoff`. Claude Code asks the user before the call. In a
   terminal ModelQuad then shows a form with the files and the task; nothing
   starts unless the user ticks the box and accepts. In the desktop app, which
   cannot show the form, Claude Code's prompt is the user's answer.
4. Codex through ModelQuad is read-only: review, exploration or architecture
   only. For changes the user wants Codex to write, suggest OpenAI's Codex
   plugin, for example `/codex:rescue`.
5. When the answer comes back, verify it before acting on it.
