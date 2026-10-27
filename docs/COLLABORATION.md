# ChatGPT and Claude

ChatGPT through Codex leads Auto mode. It answers questions or builds a validated
plan assigning development tasks to Codex or Claude with reasons. The team works
from the same project and fresh snapshots of recent chat, activity and results.
Cloud exclusions and private documents apply to both providers.

Enoy validates and applies changes, preserves backups, runs real allowlisted
checks, and sends recorded results for ChatGPT review. Claude limit/availability
failures hand the same task to Codex. There is no local-provider fallback.

Sign in through `codex login` and Claude Code. Select Auto, ChatGPT / Codex, or
Claude in Enoy. A chat with cloud access disabled cannot request a model.

Snapshots refresh between provider requests. Cancel and send a follow-up to steer
an in-flight task. Resolve pending changes before starting a new plan. Auto mode
currently accepts text only. This workflow uses the local Git checkout; it does
not synchronize GitHub issues/PRs or push changes automatically.
