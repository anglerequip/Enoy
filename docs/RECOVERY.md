# Configuration, backup and recovery

All application data lives in `%LOCALAPPDATA%\FusionAI\` (override with the
`FUSION_AI_HOME` environment variable). Your project files are never stored there,
only backups of files Enoy overwrote.

| Path | What it is |
|---|---|
| `config.json` | Settings. Edited through Settings; you can also edit it by hand |
| `config-backups\` | A copy of `config.json` from before every save (last 15 kept) |
| `fusion.db` | SQLite database: projects, tasks, messages, events, change sets, check results, what was sent to which model |
| `file-backups\task-<id>\<timestamp>\` | Original versions of project files, saved before each applied change |
| `logs\fusion.log` | Application log (secrets redacted, rotated at 2 MB) |
| `webview\`, `edge-profile\` | Window/browser cache. Safe to delete while the app is closed |

## Restore settings

1. Close Enoy.
2. Copy a file from `config-backups\` over `config.json` (or delete `config.json` to
   return to defaults).
3. Start Enoy. If the file has problems, Diagnostics lists them and the app uses
   defaults for the affected settings; the broken file is left untouched.

## Undo a change

- **Revert** on the Changes screen restores the files exactly, as long as you have not
  edited them since. If you have, Enoy refuses rather than overwriting your edits.
- Otherwise copy the original from `file-backups\task-<id>\<timestamp>\`.
- In a Git project, `git diff` / `git restore` work as usual. Enoy does not commit.

## Back up everything

Close Enoy and copy the whole `%LOCALAPPDATA%\FusionAI\` folder. To move to a new
PC, copy it back to the same place after running setup there.

## Recover a task

- *interrupted* tasks (app closed mid-task): open and click **Resume**.
- Any task: **Brief** produces a self-contained continuation summary (objective,
  plan, decisions, changed files, failed approaches, check results). Copy it into
  any tool, or use **Hand off to Claude Code**, which writes it to
  `<project>\.fusion\handoff\task-<id>.md` (git-ignored) and opens Claude Code there.

## Reset

Close Enoy and delete `fusion.db` to clear all history (projects are re-added
when opened). Delete the whole folder for a full reset. Project files are not affected.
