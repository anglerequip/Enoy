# Troubleshooting

- ChatGPT unavailable: run `codex login`, then refresh provider status in Settings.
- Claude unavailable: sign in to Claude Code. Quota errors can transfer a delegated
  task to Codex; Activity records the actual provider and reason.
- Cloud access disabled: enable cloud access for the chat/project to use a model.
  Enoy has no offline model fallback.
- Failed checks: review the real output in Activity. A result without a runnable
  test command remains explicitly unverified by tests.
- Logs: `%LOCALAPPDATA%\Enoy\logs\enoy.log`.

Start with `.venv\Scripts\python.exe -m enoy --verbose` for startup diagnostics.
