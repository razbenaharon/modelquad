---
name: modelquad-setup
description: Check that ModelQuad is set up correctly and explain any problem in plain words.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# ModelQuad setup check

Call `modelquad_doctor`. Then, for the user:

1. Say in one line whether everything is ready.
2. For each check that is not ok, explain what it means in plain words and
   give the exact command or setting that fixes it.
3. Mention, briefly, the safety settings in ModelQuad's
   docs/RECOMMENDED-SETTINGS.md: Read deny rules for secret files, a
   `.modelquadignore` for files Codex should never see, keeping ModelQuad out
   of a sensitive repository, and turning off model training with both
   providers.

This check starts no Codex work and changes no settings.
