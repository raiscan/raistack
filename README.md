# raistack

Shared, agent-agnostic skills for Codex, Claude Code, and other compatible agents.

- **agent-run**: Linux machine-wide cooperative job queue, with a Python standard-library tool and a skill. See [its README](tools/agent-run/README.md) for resource configuration and usage.
- **temporary-agents**: lifecycle guidance for delegated and persistent agent sessions.
- **cowtree** and **cow-git-worktree**: guidance for copy-on-write Git worktrees. The tool lives in [raiscan/cowtree](https://github.com/raiscan/cowtree), and is installed separately.

Clone this repository and run `./install.sh`. It links skills into `~/.agents/skills` and commands into `~/.local/bin`, and clones cowtree into `~/.local/share/cowtree` if absent. Ensure the bin directory is on PATH. Set `AGENT_SKILLS_HOME`, `RAISTACK_BIN_HOME`, or `COWTREE_HOME` to change destinations. Existing non-symlinks are protected unless `--force` is passed; back them up first. Existing symlinks may be replaced. Configure other agents to load the skills directory, or link the individual skill directories into their own skill location.

Run the synthetic tests with `cd tools/agent-run && python3 -m unittest -v`. Queue state uses `XDG_STATE_HOME` (default `~/.local/state`); installation does not modify it.
