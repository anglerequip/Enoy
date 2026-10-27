# Enoy 0.3.0 architecture

`app.build()` registers exactly two providers: Codex and Claude Code. Both use
supported signed-in command-line clients. There is no local model client or model
memory supervisor.

`coordinator.start()` evaluates a prompt with ChatGPT, validates its task plan,
and passes it to `TeamRunner`. Manual selection supports Codex or Claude only.
Every cloud request checks the chat/project cloud policy and scrubs excluded files,
private documents, quoted private content and secrets from shared state.

`TeamRunner` stores dependencies and assignments, prevents overlapping file work,
applies validated changes through review gates, runs allowlisted checks, and sends
recorded results to ChatGPT for final review. Claude quota failures hand work to
Codex. Budgets cap tasks, calls, retries and correction rounds.

The localhost HTTP API requires a per-launch token and a localhost Host header.
SQLite stores projects, chats, messages, changes, activity, team tasks and results.
The UI and Activity panel expose only Codex and Claude provider controls. Historical
messages retain the original provider ID for accurate attribution.

Historical design notes for removed workflows are in `docs/history/`.
