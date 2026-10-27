# Enoy

Enoy 0.3.0 is a Windows desktop app where ChatGPT evaluates your request,
assigns development tasks to Codex or Claude, and reviews the real results.
Both providers share recent conversation, task outputs, activity, and permitted
project files through Enoy. Dark mode is the default.

## Start

1. Run **Setup Enoy.cmd** once to install the desktop dependencies and shortcuts.
2. Install the official Codex CLI and run `codex login`.
3. Install and sign in to Claude Code to enable Claude tasks.
4. Open **Enoy** from the Desktop or Start menu. Open your code folder and use Auto.

The only providers are **ChatGPT / Codex** and **Claude**. There is no local-model
provider, model download, model server, or automatic local fallback. The previous
local-model integration has been removed. An unavailable Claude task can transfer
to Codex, with the reason recorded in Activity.

Enoy validates file changes, preserves backups, checks for conflicts, and uses your
change-approval setting. QA runs allowlisted project tests; ChatGPT reviews the
recorded outcome. Nothing commits or pushes from Enoy automatically.

Saved chats and settings live in `%LOCALAPPDATA%\Enoy` (`ENOY_HOME` overrides it).
Existing data from the earlier application name is imported without deleting the
original copy. Existing chat history retains its original provider attribution.

Cloud privacy exclusions apply to both providers. A project or chat with cloud
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
