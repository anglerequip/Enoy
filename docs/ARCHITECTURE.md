# Enoy 0.4.0 architecture

`app.build()` registers exactly two providers: Codex and Claude Code. Both use
supported signed-in command-line clients. There is no local model client or model
memory supervisor.

`coordinator.start()` evaluates a prompt with ChatGPT, validates its task plan,
and passes it to `TeamRunner`. Manual selection supports Codex or Claude only.
Every cloud request checks the chat/project cloud policy and scrubs excluded files,
private documents, quoted private content and detected secrets from shared state.
`native.py` resolves the explicit Workspace/Full Access policy and native session
scope. Native requests preserve provider tools, system instructions and supported
customizations. Codex uses workspace-write with automatic review, or the supported
Full Access sandbox policy with on-request approvals. Claude uses auto permission
review. Neither adapter uses approval-bypass flags.

`TeamRunner` stores dependencies and assignments. Native workers inspect, edit,
and verify directly; they run serially in the shared worktree. Enoy observes text
file diffs for review and records live tool outcomes. Its separate verification
runner uses allowlisted checks. Filtered context requests still use validated
patches, backups and review gates. Configured private-file filters disable native
tools because arbitrary native commands cannot enforce those filters. Claude
quota failures hand work to Codex. Budgets cap tasks, calls, retries and corrections.

`backends/process.py` streams native CLI events and cancels the process tree on
cancel or timeout. Native session IDs persist per chat/provider, scoped to the
workspace, access mode, model and effort. Full Access can start in a general chat
and discover projects using native tools. Workspace general chats have read-only
Enoy MCP folder discovery. Native tools act under Windows and provider policies;
Enoy does not grant administrator rights or override mandatory approvals.

The localhost HTTP API requires a per-launch token and a localhost Host header.
SQLite stores projects, chats, messages, changes, activity, team tasks and results.
The UI and Activity panel expose only Codex and Claude provider controls. Historical
messages retain the original provider ID for accurate attribution.

Historical design notes for removed workflows are in `docs/history/`.
