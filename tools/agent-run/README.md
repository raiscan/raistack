# agent-run

A cooperative Linux batch queue shared by every project using the same account
and state directory. Requires Python 3.9+ and a local filesystem with `flock`
support. The crash test also uses Linux pidfds (Linux 5.3+).

```sh
ln -s "$PWD/agent-run" ~/.local/bin/agent-run
agent-run --mem 4G --cores 4 --est 10m --label "Build" -- make test
agent-run --status
agent-run --stats
```

Add `~/.local/bin` to `PATH` if needed. Arguments after `--` go straight to the
command. Maven defaults to 4 cores and 4 GiB. A Java `bench run` defaults to
3 GiB and reads its core estimate from `--jobs` (4 cores when absent).
Other commands default to 4 cores and 2 GiB. Set explicit estimates for jobs
whose resource use is known. Memory accepts bytes or binary `K`, `M`, and `G`.
Duration accepts seconds or `s`, `m`, and `h` suffixes and defaults to ten
minutes. It determines queue order and has no effect on execution time.

Use `--java-cores` to append `-XX:ActiveProcessorCount=<cores>` to the child's
`JAVA_TOOL_OPTIONS`. Tune worker counts and JVM limits in the command when
needed. Reservations and live checks are advisory. Commands launched outside
the runner, or jobs that use more than their estimates, can still saturate the
machine.

## Budget and ordering

All agents on this account should use the same `XDG_STATE_HOME`. State lives in
`${XDG_STATE_HOME:-~/.local/state}/agent-run/`. From the host's online CPU count
and Linux `MemTotal`, the default reserve is the smaller of one quarter of the resource or 4 cores / 6 GiB respectively. Admission considers both outstanding
reservations and current `MemAvailable` and one-minute load. A request larger
than the static budget fails with an error.

Override defaults by creating `config.json` in the state directory. Every key
is optional. Example with lower caps and the default desktop reserves and aging:

```json
{
  "reserve_cores": 4,
  "reserve_mem": "6G",
  "max_cores": 20,
  "max_mem": "18G",
  "aging_seconds": 300
}
```

Limits are reread during admission. Publish config changes by replacing the file
atomically. Short estimates get priority and smaller jobs can fill spare
capacity. After five minutes a waiter gets arrival-order priority. If the
oldest aged job cannot fit yet, the queue drains capacity for it.

## Leases and observation

The launcher forwards INT, TERM, HUP and QUIT to the child process group and
returns the child's exit code (128 plus the signal for signal exits). Use it
for foreground batch commands, including commands with redirected stdin.
The launched process group has no controlling terminal.

Linux closes leases on exit. The launcher and command hold the execution lease
jointly, so a killed launcher leaves a surviving child accounted for. A wrapper
that starts a worker can preserve the descriptor identified by
`AGENT_RUN_LEASE_FD` across exec (in Python, pass it in `subprocess`'s
`pass_fds`). Keep the command in the foreground until its work is complete.
Keep the state directory and slot files in place while the queue is in use.

`--status` shows running and waiting labels, requesting PIDs, elapsed times,
reservations, and directories. `--stats` summarizes completed wait/hold times
and exit codes from `telemetry.jsonl`. A launcher killed before completion
cannot write a completion event. No external service or dashboard is required.

## Tests

```sh
python3 -m unittest -v
```

The suite uses independent working directories, real flock leases and tiny
Python children. Host counter samples are simulated for repeatable admission
tests.
