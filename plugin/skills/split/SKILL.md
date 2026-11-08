---
name: split
description: Propose which steps of a task Claude does and which read-only steps Codex could take. Starts nothing.
argument-hint: "[the task]"
disable-model-invocation: true
metadata:
  managed-by: modelquad
---

# Who does what

The user asked for a proposed division of this task between Claude and Codex:
$ARGUMENTS

1. Break the task into 1 to 12 steps. For each, give a short title, its task
   kind (implementation, testing, debugging, review, exploration, architecture
   or trivial), what it needs (`edit` if it changes files, `run` if it runs
   commands or tests, `none` for reading and thinking only), and
   `independent: true` for a review of Claude's own work or another opinion
   that should not come from Claude. Give `size: small` for a step that needs
   little reading or thinking and `large` for a big one (default `medium`); a
   small step is never proposed for Codex because of quota.
2. Call `modelquad_split` with this session's absolute project directory as
   `working_directory` and the steps. It starts nothing.
3. Show the user its lines as written: who takes each step, then the quota
   and account facts.
4. Do the Claude steps as usual. When you reach a Codex step, ask the user;
   call `modelquad_delegate` for that one step only if they agree. Claude Code
   shows its approval prompt before each call. The proposal approves nothing.
   If the reply gives a step `trigger: split_quota`, pass that trigger to
   `modelquad_delegate` for that step.
