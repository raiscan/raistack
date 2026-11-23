---
name: agent-run
description: Use the shared machine queue before launching heavy local work.
---

# Shared machine resources

Run heavy local commands through `agent-run` from their normal working directory.
Specify `--cores`, `--mem`, and `--est` to suit the workload. Use
`agent-run --status` and `agent-run --stats` to inspect the queue. Keep the
same `XDG_STATE_HOME` across cooperating agents and keep queued commands alive.
See the tool README for configuration and usage.
