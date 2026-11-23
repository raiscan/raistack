# agent-run

A cooperative Linux batch queue shared by every project using the same account
and state directory. Requires Python 3.9+ and a local filesystem with `flock`
support. The crash test also uses Linux pidfds (Linux 5.3+).

```sh
ln -s "$PWD/agent-run" ~/.local/bin/agent-run
agent-run --mem 4G --cores 4 --est 10m --label "Build" -- make test
agent-run --vram 10G --cores 2 --mem 4G --label "Whisper" -- python transcribe.py
agent-run --status
agent-run --stats
agent-run --dashboard
agent-run --update
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

## GPU memory

Pass `--vram SIZE` for any job that loads a model or other work onto the GPU.
VRAM is reserved like cores and memory: jobs whose combined `--vram` exceeds
the GPU budget wait, and the live check also waits while the GPU's measured
free VRAM (minus the reserve) is below the request. Jobs without `--vram` never
wait for the GPU. A `--vram` request fails immediately when no GPU budget is
available or the request is larger than the budget.

The GPU is read from the amdgpu sysfs counters
(`/sys/class/drm/card*/device/mem_info_vram_*`). By default the card with the
most VRAM is used, which skips integrated graphics. Set `gpu` in `config.json`
to a card name such as `"card1"` or a PCI address such as `"0000:03:00.0"`
(stable across boots) to choose one. The default reserve is the smaller of
1 GiB or one eighth of VRAM, on top of whatever the desktop is already using.
`reserve_vram` and `max_vram` override it. Other GPU vendors are not detected
yet, so `--vram` requests fail on them.

Add `--gpu-exclusive` when a job should have the GPU to itself, for example a
model run that would otherwise share GPU compute with another model. It
waits until no other `--vram` or exclusive job is running, and while it runs
no other GPU job starts. Jobs without GPU flags are unaffected. Combine it with
`--vram` to also apply the VRAM checks.

Reservations only cover jobs that go through agent-run. A model server that
stays loaded outside the queue (for example a resident Ollama or llama.cpp
server) shows up only through the live check. A job that keeps a model resident
after its command exits is not accounted for.

## Updating

`agent-run --update` fast-forwards the Git checkout that holds the running
`agent_run.py` to its branch's upstream. The checkout is found by resolving
symlinks, so it works wherever the clone or the command link lives. Local
commits or conflicting edits stop the update and leave the checkout
unchanged. Run `install.sh` again when the update reports that it changed,
and restart a running dashboard to load the new version.

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
  "aging_seconds": 300,
  "gpu": "0000:03:00.0",
  "reserve_vram": "1G"
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
cannot write a completion event.

## Dashboard

Run `agent-run --dashboard` and open `http://127.0.0.1:8767/`. Use
`agent-run --dashboard --port 8770` to choose another port, or use port
`0` to have Linux pick a free port and open the URL printed by the command.
Stop the server with Ctrl-C. The page and its `/api/status` JSON endpoint
are available only on IPv4 loopback.

The page refreshes every two seconds. It shows the configured budget,
desktop reserves, busy CPU cores, Linux load and available memory, active reservations,
running leases with process ancestry, waiters with observed admission
reasons, recent completion timings, and processes outside agent-run ranked
by sampled CPU and resident memory. Consecutive waiters with the same
label, command, directory, reservation and admission reason are folded
into one row with a count. A job's launching agent is the first
recognised agent process in its ancestry; the full ancestry is in the
tooltip. CPU is measured over consecutive
samples, with 100% equal to one logical core. The queue view is a lock-free
snapshot, so entries and reasons can change between refreshes. The dashboard
reads existing files and Linux counters without creating state, taking queue
locks, or removing stale records.

New leases store a display command with the executable basename and
recognized build operations. Other arguments are represented by an ellipsis
so arbitrary argument values stay out of leases, telemetry, and the page.
Older leases can recover a display command from the live holder when Linux
exposes it. Process ancestry uses process names and PIDs. Environment values
are never read. Keep labels and working-directory names suitable for local
display, since they are included in queue and telemetry metadata.

To include OpenGGF-style Maven queues, add an optional `maven_repos` list to
the existing `config.json` alongside any resource limits:

```json
{
  "maven_repos": ["/absolute/path/to/repository"]
}
```

Use absolute paths to repositories or linked worktrees. The adapter reads
running leases and waiting requests from the shared Git directory, including
fresh admission reasons published by the Maven scheduler. Maven queue
records are displayed independently of agent-run accounting.

## Tests

```sh
python3 -m unittest -v
```

The suite uses independent working directories, real flock leases and tiny
Python children. Host counter samples are simulated for repeatable admission
tests.
