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
3. Call `modelquad_handoff`. Claude Code shows its approval prompt before the
   call. In a terminal ModelQuad then shows the handoff form with the files and
   the task; nothing starts unless the user ticks the box and accepts. In the
   desktop app's Code tab, which shows no forms, Claude Code's approval prompt
   is the user's answer, and Codex then starts. In any other Claude Code that
   cannot show the form, nothing starts and the reply says what to do next.
4. Codex through ModelQuad is read-only: review, exploration or architecture
   only. Claude makes the code changes itself; Codex only reads and advises.
5. If the reply's first line says Codex was not started, tell the user that
   line and the next one as written, and start nothing else.
6. When the answer comes back, verify it before acting on it.
