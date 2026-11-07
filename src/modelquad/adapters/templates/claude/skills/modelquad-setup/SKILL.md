---
name: modelquad-setup
description: Check ModelQuad's setup and explain each problem in plain words, with the command that fixes it.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# ModelQuad setup check

Call `modelquad_doctor`. Then, for the user:

1. Say in one line whether everything is ready.
2. For each check that is not ok, explain what it means in plain words and
   give the exact command or setting that fixes it.
3. Mention, briefly, the privacy settings in ModelQuad's
   docs/RECOMMENDED-SETTINGS.md: Read deny rules for secret files, a
   `.modelquadignore` for files Codex should never see, keeping ModelQuad out
   of a sensitive repository, and turning off model training with both
   providers.

4. For your plans and this repository's `.modelquad.toml` (valid, invalid
   with its fix, or unknown keys), suggest the user runs `modelquad doctor`
   in a terminal: those details are shown to the person, not to Claude.

This check starts no Codex work and changes no settings.
