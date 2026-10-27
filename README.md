# Enoy

Enoy 0.4.0 is a Windows desktop app where ChatGPT evaluates your request,
assigns development tasks to Codex or Claude, and reviews the real results.
Both providers share recent conversation, attached images, task outputs, activity,
and permitted project files through Enoy. Dark mode is the default.

## Start

1. Run **Setup Enoy.cmd** once to install the desktop dependencies and shortcuts.
2. Install the official Codex CLI and run `codex login`.
3. Install and sign in to Claude Code to enable Claude tasks.
4. Open **Enoy** from the Desktop or Start menu. Open your code folder and use Auto.

The only providers are **ChatGPT / Codex** and **Claude**. There is no local-model
provider, model download, model server, or automatic local fallback. The previous
local-model integration has been removed. An unavailable Claude task can transfer
to Codex, with the reason recorded in Activity.

Native project tools are enabled by default for both providers. In a selected
project, ChatGPT and Claude can inspect files, edit code, and execute commands
and tests. Codex uses its workspace-write sandbox and automatic approval review;
Claude uses its auto permission mode. Tool activity appears in Activity while
the request runs, including command outcomes and output previews.
Settings → Models provides **Workspace Access** (the default) and explicitly
enabled **Full Access**. Workspace Access works in the selected project; general
chats have folder discovery and authoritative Enoy source paths. Full Access also
works from general chats and authorizes agents to find and work on other projects
under Enoy's Windows account. Codex uses its supported `danger-full-access`
sandbox policy with `on-request` approvals; Claude retains automatic permission
review. Neither mode bypasses provider approvals, Windows permissions, or grants
administrator rights. Provider policies can still reject an action; the actual
failure is shown in the chat. A CLI approval that cannot be handled in print mode
is a reported limitation, not an approved action.

Native provider sessions continue across follow-up messages, scoped to the chat,
workspace, model, reasoning setting and permission mode. Switching that scope
starts a fresh native session; both providers still receive Enoy's shared chat,
task and activity context. Large shared-context records explicitly mark omitted
text; native session history and tool results remain managed by the provider.
ChatGPT defaults to gpt-6.1-sol with
high reasoning effort; model and effort are configurable under Settings → Models.

Native edits are direct filesystem changes and do not create Enoy undo backups.
Delegated development tasks also use native tools, with observed filesystem diffs
returned to the coordinator for review. Native tasks share a working tree and run
one at a time so changes can be attributed reliably. The provider can use its own
supported tools and workflows within each task. Disabling native tools restores
patch proposals with Enoy's validation, backups, conflict checks and approvals.
No automatic commit or push is requested by Enoy.

Saved chats and settings live in `%LOCALAPPDATA%\Enoy` (`ENOY_HOME` overrides it).
Existing data from the earlier application name is imported without deleting the
original copy. Existing chat history retains its original provider attribution.

Cloud privacy exclusions apply to both providers. Configured private file filters
or private project documents disable native tools, preserving filtered context
access instead. A project or chat with cloud
access disabled can still be browsed and managed, but cannot make model requests.

## Development

```
.venv\Scripts\python.exe -m enoy --verbose
.venv\Scripts\python.exe -m pytest -q
```

The current suite tests the cloud workflow, adapters, permissions, storage,
projects, artifacts and APIs with scripted providers. Tests of removed local-model
features are retained under `tests/retired/` and are excluded from collection.

See [Architecture](docs/ARCHITECTURE.md), [Collaboration](docs/COLLABORATION.md),
[Recovery](docs/RECOVERY.md), and [Troubleshooting](docs/TROUBLESHOOTING.md).
