# ChatGPT and Claude

ChatGPT through Codex leads Auto mode. It answers questions or builds a validated
plan assigning development tasks to Codex or Claude with reasons. The team works
from the same project and fresh snapshots of recent chat, activity and results.
Cloud exclusions and private documents apply to both providers.

Native agents inspect files, edit code, and run commands and tests directly.
Delegated native workers complete assigned tasks with their own native tools;
Enoy records activity and observes filesystem diffs for ChatGPT review. Direct
edits have no Enoy undo backup. With native tools disabled or private filters
configured, Enoy validates patch proposals, preserves backups and asks for approval.
Its separate verification runner runs real allowlisted checks. Claude limit/availability
failures hand the same task to Codex. There is no local-provider fallback.

Sign in through `codex login` and Claude Code. Select Auto, ChatGPT / Codex, or
Claude in Enoy. A chat with cloud access disabled cannot request a model.

Snapshots refresh between provider requests. Cancel and send a follow-up to steer
an in-flight task. Resolve pending changes before starting a new plan.
Native sessions continue between follow-ups in the same access scope. Both
providers receive shared chat, task and activity records. Long context records
mark omissions instead of producing silently truncated JSON.

Settings → Models offers Workspace Access and Full Access. Full Access must be
explicitly selected; it supports finding and working on projects from a general
chat. Provider approvals and Windows permissions apply in both modes. Private-file
filters retain filtered context mode so those exclusions can be enforced.

Image attachments are delivered to the coordinator, workers and final reviewer,
including follow-ups that refer to earlier screenshots. This workflow uses the
local Git checkout; it does not synchronize GitHub issues/PRs or push changes
automatically.
