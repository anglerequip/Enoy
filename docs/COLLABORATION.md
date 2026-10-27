# ChatGPT and Claude in Enoy

Enoy's Auto mode now starts with ChatGPT through the official Codex CLI. ChatGPT
evaluates your prompt, answers questions directly, or produces a dependency-aware
plan assigning each development task to `codex` or `claude` with a reason.

Both providers receive a fresh snapshot of recent conversation, team messages,
task outputs, and activity before each request. The snapshot is bounded, rather
than an unlimited transcript or a connection to your open Codex desktop chat.
Claude can read permitted project files through Enoy's existing MCP tools; Codex
can request additional files with Enoy's bounded READ protocol. Each works from
the same local project folder and sees applied changes on its next request.

Enoy validates proposed edits, checks for file conflicts, keeps backups, and uses
the existing change approval setting. Tasks touching the same file are serialized.
QA runs the project's allowlisted checks. ChatGPT receives the actual results and
diffs, then summarizes them or requests a bounded correction round. The existing
Activity panel shows assignments, reasons, provider calls, messages, and results.

## Start

1. Install the official Codex CLI if needed and run `codex login` in a terminal.
   Enoy uses the CLI's authentication; it never reads your credentials.
2. Install and sign in to Claude Code to enable Claude assignments.
3. Restart Enoy from the normal shortcut. Open your existing local GitHub checkout.
4. Select **Auto**. In **Settings → Who answers**, **ChatGPT leads Enoy** is enabled
   by default. You can set the Codex executable, model, and timeout there.
5. Send a text prompt. Review changes when your change approval setting requests it.

An unavailable or declined Claude assignment moves to Codex. A missing Codex CLI
stops the coordinator with an error; it does not silently impersonate ChatGPT using
another provider. Selecting Qwen or Claude manually uses the older direct workflow.
Local-only chats/projects and zero-paid mode also keep their older workflow.

## Current boundaries

- This is the first implementation of the rebuild, retaining the existing desktop
  app, database, task scheduler, and change review interface.
- Shared state refreshes at provider request boundaries. An in-flight model call
  does not receive new database events automatically. Cancel, then send a follow-up
  to redirect it; resolve pending changes before starting a new plan.
- The coordinator accepts text. Image messages can use the direct Claude workflow.
- Both models work against the same local Git checkout through Enoy. This release
  does not add GitHub issue/PR synchronization, automatic commits, or pushes.
- Cloud exclusions and private documents apply to both providers. Local-only work
  never enters the coordinator. Shared history is scrubbed before transmission.
- Codex runs without personal CLI configuration, in an empty working directory,
  with its shell tool disabled and a read-only sandbox. File access and changes
  are mediated by Enoy. This is not unrestricted native agent access to your PC.
- Existing team request and repair budgets still apply. Checks which could not run
  remain identified as unverified in the recorded results.

Codex integration reference: [official non-interactive mode documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
