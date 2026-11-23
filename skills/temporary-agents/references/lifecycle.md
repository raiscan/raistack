# Native sessions and cleanup

Use this reference only for explicitly requested native CLI sessions or when T3 Orchestration delegation is unavailable. A parent/coordinator invokes installed CLIs and relays responses. An agent is persistent through its recorded identity, conversation, and task state even when its process has exited.

## Registry

Use a UUID for the run and a unique ID for each agent. Create run directories with mode `0700` and records with mode `0600`. Keep `manifest.json`, `messages/`, and any launch logs under the run directory. Record at least:

- Run ID, owning task/session, project path, task scope, creation and expiry times in UTC.
- For each agent: ID, role, provider, exact native conversation ID, any separate management ID, workspace/worktree, state, deadline, and allowed work. Record whether the run exclusively owns the workspace and its provider state; do not infer this from a directory name.
- Owned processes or native workers, including their identity and lifecycle mechanism; never rely on a PID alone to identify a process after a restart.
- Generated artifact paths, results to preserve, and repository changes/branches that must be accounted for before cleanup.

Register a planned agent before launch. Capture its returned ID immediately in the record and retain launch output for recovery if the parent exits between launch and registration. A launching record without an ID requires investigation before another launch. Prefer one writer/coordinator per run; lock the manifest for any concurrent writer. Write updates to a temporary file and atomically replace the destination.

Expiry defaults to 24 hours. Extend it only while the task still needs the agent, recording the new deadline and reason. Expired work is not automatically accepted or resumed. Check known runs before reuse and at work checkpoints. Inspect an expired run's native state and ownership before cleanup; do not delete an active or unrelated session merely because a timestamp has passed. Recover an abandoned run only for the same authorized task, with recorded ownership transfer.

## Messages

Store immutable message files named by UUID. Each contains `id`, `from`, `to`, `task_id`, `reply_to` (or null), `text`, `created_at`, and `expires_at`. Results additionally identify their outcome: `completed`, `input_required`, `rejected`, or `failed`. A timeout or uncertain submission remains pending until status is reconciled.

Keep the same message ID on retries and reject changed payloads under that ID. The coordinator records queued/sent/answered state separately and correlates the native response with its request. This record supports reconciliation; it does not promise exactly-once execution. Do not blindly retry tasks with external side effects. Relay an actual response from one provider as peer data in a request to the other, preserving its source and message ID.

Only one invocation may own an agent's active turn. A validated adapter may inject messages through trusted tool/turn hooks or native steering without launching a second invocation. Otherwise queue messages until the active turn finishes. Never use a global "last conversation" selector for coordination. Task acceptance, transport acknowledgement, and completion are separate outcomes. The model can receive ordinary text while the coordinator manages this bookkeeping.

## Codex

If a registered agent has a separate `native_home`, use that exact provider-state namespace for native resume, inspection, archive, or deletion. Keep the home with the native ID in the registry; an agent without a recorded home retains its original namespace.

These command families were checked against the installed `codex-cli 0.160.0`. Verify `--help` if the installed version changes. Use the existing configured model or a model available to the user; the following examples use a read-only worker. Run from its registered workspace.

```bash
codex exec --json --sandbox read-only -
codex --sandbox read-only exec resume <SESSION_UUID> --json -
codex queue --thread <SESSION_UUID> --message <MESSAGE_TEXT>
codex delete <SESSION_UUID> --force
```

Supply prompts through stdin for the commands ending in `-`. Outside a Git checkout, add `--skip-git-repo-check` only for a known scratch directory. Do not pass `--ephemeral` for persistent agents. Capture `thread_id` from the `thread.started` JSONL event and capture completed agent-message events as responses. Check exit status and `turn.failed`/`error` events.

`codex queue` can submit to an existing session, but its acknowledgement is not a returned answer. Use direct `exec resume` for the simplest synchronous exchange. `codex archive` keeps the conversation and is retention, not deletion. Before `delete --force`, verify the exact UUID belongs to this temporary run and its useful result is exported; this UUID-only flag is for already authorized cleanup, not broader permission bypass. Never delete provider storage databases or shared session directories by hand.

## Claude Code

If a registered agent has a separate `native_home`, use that exact provider-state namespace for native commands. Inspect purge previews in the same home used to create the session; the normal user project list is a separate namespace.

These command families were checked against the installed Claude Code `2.1.287`. Prefer synchronous saved conversations in a private workspace owned exclusively by the run. That permits native project-state cleanup without touching another agent's conversation. Register a unique conversation UUID before launch and keep using the same workspace:

```bash
claude --print --session-id <CONVERSATION_UUID> --output-format json
claude --print --resume <CONVERSATION_UUID> --output-format json
claude purge --dry-run <EXCLUSIVELY_OWNED_WORKSPACE>
claude purge --yes <EXCLUSIVELY_OWNED_WORKSPACE>
```

Supply each message on stdin. Check `session_id`, `is_error`, and the returned result. The process exits after each turn while the conversation stays saved. Do not pass `--no-session-persistence`. For a text-only worker, use `--tools ""`, `--strict-mcp-config`, and an appropriate noninteractive permission mode. Use the existing authentication and permissions; permission bypass flags are unnecessary.

Before project-state purge, verify exclusive ownership, inspect the dry run, ensure every affected conversation/artifact belongs to the run, and preserve all useful results and changes. An agent can operate from its private scratch directory and receive project information as text or authorized read access. An isolated worktree can also be exclusively owned, subject to repository cleanup rules. **Never purge a shared project, the user's home, or all projects.** If the preview contains unknown state or ownership cannot be established, keep `cleanup_pending` and use a narrower native lifecycle if available.

If an agent must work directly in a shared workspace, a managed session offers targeted stop/remove. Its launcher requires that workspace to be trusted already; this skill does not authorize changing trust settings. From that registered workspace:

```bash
claude --bg --session-id <CONVERSATION_UUID> --name <UNIQUE_NAME> <TASK_TEXT>
claude agents --all --json
claude logs <MANAGEMENT_ID>
claude --bg --resume <MANAGEMENT_ID> <MESSAGE_TEXT>
claude stop <MANAGEMENT_ID>
claude rm <MANAGEMENT_ID>
```

Preselect a conversation UUID and capture the management ID returned by `--bg`. Record both if they differ. Choose tools and permission mode consistent with the actual task. The `agents --json` inventory mixes interactive and background entries; only background entries necessarily have a management `id`. Match exact IDs and the conversation's `sessionId` rather than assuming every item is a worker.

Wait for a completed turn and run `claude stop <MANAGEMENT_ID>` before leaving the agent idle. Resume only a stopped session: the CLI says resuming a session that is already running may create a copy. Capture and register any replacement ID rather than assuming it is the original. Use `agents` and `logs` to inspect only the registered session. `attach` opens an interactive session; it is not a programmatic result API.

For synchronous messages to a stopped managed conversation, the same print/resume command can be used. A standalone print session is not necessarily a managed background session: do not assume `claude rm` can remove it. For standalone sessions use project-state purge only within the exclusive private-workspace boundary above. If native deletion cannot remove all owned temporary state, record the remainder as `cleanup_pending` instead of deleting shared caches manually.

`stop` preserves the conversation. `rm` removes the background session and can remove an associated worktree. Inspect and account for worktree changes first. Refusals protecting dirty or unpushed work require preservation/integration, not `--discard-unpushed` or force-removal flags.

## Cleanup sequence

1. Mark the run as finishing and stop accepting new work. Resolve or record pending deliveries, cancellations, and uncertain side effects.
2. Export accepted results to the requested project/destination. Record unresolved work without discarding it.
3. Stop only this run's owned processes/workers and verify they stopped. Use a native lifecycle command where available; custom processes require verified identity and a bounded termination procedure.
4. Check each worktree for unknown, uncommitted, or unmerged work and account for it under repository delivery rules before deleting provider history. Delete only the exact registered temporary native sessions, or Claude project state in an exclusively owned workspace after inspecting the dry run. Reconcile already-missing state rather than treating it as proof that outputs were saved.
5. Remove a worktree and any local task branch only when every change is accounted for. Never switch the main workspace's branch.
6. Remove registered generated files and the run directory when no blockers remain. Write a receipt under `~/.local/state/temporary-agents/receipts/<run-UUID>.json` with the IDs, preserved-result locations, outcome, and any remaining blockers; omit transcripts and secrets. Receipts older than seven days may be pruned if they contain no unresolved cleanup or user-required records.

If cleanup is refused or unsafe, keep the manifest and label the relevant agent/run `cleanup_pending`. Future coordination checkpoints can revisit it. An instruction file does not run a reaper after a crash: hard automatic expiry or continuous workers require an explicitly implemented scheduler/supervisor. Suggest saved sessions as the lower-complexity alternative when that infrastructure is unnecessary.
