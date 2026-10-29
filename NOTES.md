# Build journal (harness-improvement log)

Run start: 2026-10-29 ~19:00 local. Platform: Claude Code (VS Code extension), bypass permissions.

- **Intake**: AskUserQuestion worked for all 7 rounds. Free-text answers in "Other" carried key scope decisions (admin-only generation, no mid-run tagging) — the intake should expect that and read Other text carefully.
- **Key check**: Anthropic org-level key rejected with 400 "not scoped to a workspace" → added `HERO_ANTHROPIC_WORKSPACE_ID` and the `anthropic-workspace-id` header. Lesson: the key-validation script should detect this error class and ask for the workspace ID instead of a new key.
- **Session restart** mid-intake (user closed VS Code) was harmless because nothing was in flight; cwd resets to $HOME per Bash call — always use absolute paths.
