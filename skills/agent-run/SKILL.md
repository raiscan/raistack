---
name: agent-run
description: Use the shared machine queue before launching heavy local work.
---

# Shared machine resources

Run heavy local commands through `agent-run` from their normal working directory.
Specify `--cores`, `--mem`, and `--est` to suit the workload. Add `--vram`
(such as `--vram 8G`) to any command that runs a model or other work on the
GPU, so GPU jobs from different agents wait for each other instead of
running out of VRAM. Add `--gpu-exclusive` when the job should have the GPU
to itself. Use
`agent-run --status` and `agent-run --stats` to inspect the queue. Keep the
same `XDG_STATE_HOME` across cooperating agents and keep queued commands alive.
For a read-only browser view across projects, start `agent-run --dashboard`
and open `http://127.0.0.1:8767/` on the same machine. Use `--port N` if the
default port is in use. Stop the dashboard with Ctrl-C.
See the tool README for configuration and usage.
