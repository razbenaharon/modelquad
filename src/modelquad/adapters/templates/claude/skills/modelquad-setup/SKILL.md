---
name: modelquad-setup
description: Check ModelQuad's setup and explain each problem in plain words, with the command that fixes it.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# ModelQuad setup check

Run this with the Bash tool, with this session's absolute project directory.
It changes nothing and starts no review:

    modelquad doctor --project "<this session's absolute project directory>"

Then, for the user:

1. Say in one line whether a review can run.
2. For each check that is not ok, explain what it means in plain words and
   give the exact command or setting that fixes it. Where a check says
   unknown, say unknown; do not guess.
3. Mention, briefly, the privacy settings in ModelQuad's
   docs/RECOMMENDED-SETTINGS.md: Read deny rules for secret files, a
   `.modelquadignore` for files Codex should never see, keeping ModelQuad out
   of a sensitive repository, and turning off model training with both
   providers.

Run no other command and change no setting.
