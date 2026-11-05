---
name: settings
description: Open ModelQuad's settings page in the user's browser, where the user chooses who decides to ask Codex, this project's own choice and their plans. Claude changes nothing itself.
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# ModelQuad settings page

Run this with the Bash tool in the background (it keeps running until the user
presses Done on the page), with this session's absolute project directory:

    modelquad settings open --project "<this session's absolute project directory>"

Then tell the user, in one line, that the settings page is open in their
browser, that each choice is saved as they make it, and that they press Done
when they finish. If the command printed an address instead, give the user
that address to open.

The user makes every choice on the page. Do not change a setting yourself:
do not open, read or press anything on the page, and do not run another
`modelquad settings` command unless the user asks for that.
