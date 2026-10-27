# Enoy 0.3.0 verification

- The current regression suite passed 208 tests. Cloud-only provider contract and
  old-chat continuation checks passed separately with the coordinator tests.
- A minimal live Codex request through `app.build()` completed with
  `ENOY_CLOUD_OK`, using only the registered Codex and Claude providers.
- The installed local model was removed using the model server's removal command.
  The registered application uninstaller returned zero. Its executable, model
  folder, running processes and user PATH entry were checked after removal.
- Saved chats and project data were preserved. Old chat messages keep their original
  provider attribution; new work uses Codex or Claude only.
- UI JavaScript syntax and Python module compilation passed.

Historical local-provider tests and verification reports are retained under
`tests/retired/` and `docs/history/`; they do not describe the active runtime.
