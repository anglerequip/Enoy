# Enoy 0.4.2 verification

- **291 automated tests passed** in the combined working copy. New tests cover
  Claude plan approval and revision before execution, complex-task routing,
  first repairs, final result review, consent/decline, quota fallback, Full Access
  delegation, blocked implementations and missing completion verdicts.
- A live Enoy HTTP workflow confirmed Claude's weekly quota failure. It reported
  that the plan was not Claude-reviewed, used Luna for the bounded edit and shell
  verification, then used Sol for the final review with an explicit fallback log.
  The actual file-content assertions passed. **Claude live review remains
  unverified because the account has no available quota.**
- A filtered Codex configuration error exposed by concurrent Blender changes was
  repaired: requests that ignore user config no longer add a disabled Blender
  server entry without a transport. The Blender and hybrid tests passed together.
- Native workers reporting a blocker without observed file changes no longer
  count as successful implementation. The new review workflow also requires an
  explicit final completion verdict.
- GPT planning, Claude plan/result review, Claude complex work and repairs, and
  automatic Claude participation were enabled. Enoy was restarted only after it
  reported no active work, respecting the user's instruction to let task #19 finish.

## Enoy 0.4.1 verification

- **256 automated tests passed** after introducing phase-based Sol/Luna routing,
  per-request CLI model/effort settings, session separation, fallback handling,
  public progress updates and existing workflow regressions.
- A live task through Enoy's authenticated HTTP API used **GPT-6.1 Sol medium →
  GPT-6 Luna high → GPT-6.1 Sol medium**. Luna edited the temporary project's
  README, verified the replacement with a real shell command, and returned its
  observed diff for Sol's review. All model/effort and progress assertions passed.
- UI checks verified model labels against historical transmission timestamps,
  expanded progress summaries beneath response text, escaping of tool content,
  and removal of the obsolete local-model loading message. JavaScript syntax passed.
- Public assistant updates and actual tool events populate progress. Raw private
  reasoning events are excluded. Displaying the progress panel adds no separate
  model request. Model selection does not guarantee fewer total tokens.

## Enoy 0.4.0 verification

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

## 0.5.0 distribution and workspace checks

- The final suite passed **329 tests** on Windows, including update integrity,
  busy-task handling, API authentication, account usage parsing, chat model defaults,
  and the existing provider/workflow regressions.
- Built the x64 desktop runtime and Windows installer. Installed into an isolated
  test folder with provider downloads, shortcuts and uninstaller registration disabled.
  The installed app served its UI, initialized its native WebView2 window/Windows
  frame, and answered the stdio MCP initialize handshake successfully.
- Visually checked Chat and Code, including their mode labels and different surface
  styles, and the Settings setup/update controls. Screenshots are in `docs/screenshots`.
- Queried the installed Claude CLI's read-only usage control protocol successfully;
  it reported the account's actual five-hour and weekly subscription windows.
- First-time provider downloads and new-account browser sign-in have not been tested
  on a clean Windows VM. The app installer is not Authenticode-signed. Tests do not
  constitute a complete security audit or guarantee provider/model availability.

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
