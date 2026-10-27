# Enoy 0.4.0 verification

Verified on Windows on 2026-10-27:

- **238 automated tests passed**, including permission-mode API round trips,
  both native adapters, streamed events, cancellation, session scope, native
  delegated workers, observed diffs, privacy filters and existing patch workflows.
- UI JavaScript syntax check passed.
- A live Codex chat through Enoy's authenticated HTTP API read an existing file,
  created and modified another file, ran a shell verification, and returned
  command activity. The Enoy process read and modified the agent-created file;
  a follow-up resumed the same native session, read Enoy's edit, wrote again,
  and verified the final contents. All assertions passed.
- Full Access from a general chat discovered and edited the temporary project
  outside the selected chat folder and verified the generated file. No project
  selection or approval-bypass flag was needed.
- A live Auto request produced a coordinator plan, ran a native Codex worker,
  recorded the actual disk diff and shell outcome, and returned through final
  coordinator review. Enoy's separate QA correctly reported that this temporary
  fixture had no allowlisted project test command.
- Real Codex called Enoy's MCP folder-discovery tool in filtered context mode
  and returned the authoritative Enoy source directory.
- The approved Windows ACL repair grants the current Windows account inherited
  Modify rights on this checkout and its protected test-cache directory. The
  two-way live file test passed afterward. No unrelated account was granted access.
- **Claude's live edit was not verified**: its signed-in account reported a weekly
  usage limit. This is an account limitation, distinct from file permissions.
  Its native argument contract, subprocess execution, events, task integration
  and session scoping passed automated tests.
- Running Enoy reported version **0.4.0**, native tools enabled, **Full Access**,
  an operational authenticated status API and no configuration warnings.

Provider approval checks and Windows access controls remain active. CLI approval
requests requiring an interactive host are not bridged into Enoy in this release;
such failures must be reported rather than represented as successful actions.
Private file filters intentionally retain filtered context mode. Observed team
diffs cover indexed readable text files up to 200 KB each and 16 MB per snapshot;
native tool activity covers operations beyond that diff preview.

## Earlier migration checks

- The 0.3.1 regression suite passed 215 tests. Cloud-only provider contract and
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

- Image workflow checks cover Auto, manual Claude approval, worker handoffs,
  follow-ups, retries, missing files and temporary attachment cleanup. A live
  Auto-mode request described the supplied error screenshot correctly.
