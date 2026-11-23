"""A shared Linux admission queue for cooperative batch jobs."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

GIB = 1 << 30
SLOTS = 64
POLL_SECONDS = .25
LEASE_ENV = "AGENT_RUN_LEASE_FD"
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


def quantity(value, multipliers):
    match = re.fullmatch(r"([1-9][0-9]*)([A-Za-z]*)", value)
    if not match or match[2].upper() not in multipliers:
        raise ValueError(f"Invalid quantity {value!r}")
    return int(match[1]) * multipliers[match[2].upper()]


def memory(value):
    return quantity(value, {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": GIB})


def duration(value):
    return quantity(value, {"": 1, "S": 1, "M": 60, "H": 3600})


def display_memory(amount):
    for unit, size in (("G", GIB), ("M", 1 << 20), ("K", 1 << 10)):
        if amount >= size:
            return f"{amount / size:.3g}{unit}"
    return f"{amount}B"


def resources(cores, mem):
    word = "core" if cores == 1 else "cores"
    return f"{cores} {word} / {display_memory(mem)}"


def host_resources():
    mem = {}
    with open("/proc/meminfo") as info:
        for line in info:
            key, _, value = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                mem[key] = int(value.split()[0]) << 10
    return os.sysconf("SC_NPROCESSORS_ONLN"), mem["MemTotal"], mem["MemAvailable"], os.getloadavg()[0]


@dataclass(frozen=True)
class Budget:
    cores: int
    mem: int
    live_cores: float
    live_mem: int
    aging: int

    def allows(self, job):
        return job["cores"] <= self.cores and job["mem"] <= self.mem


def read_budget(config_path):
    try:
        config = json.loads(config_path.read_text())
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    unknown = config.keys() - {"reserve_cores", "reserve_mem", "max_cores", "max_mem", "aging_seconds"}
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")

    def integer(key, default, minimum):
        value = config.get(key, default)
        if type(value) is not int or value < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
        return value

    def size(key, default, allow_zero=False):
        value = config.get(key, default)
        if allow_zero and value in ("0", "0B", "0b"):
            return 0
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a size string such as 6G")
        return memory(value)

    total_cores, total_mem, available, load = host_resources()
    # Leave enough capacity for at least one small job on small machines.
    reserve_cores = integer("reserve_cores", min(4, total_cores // 4), 0)
    reserve_mem = size("reserve_mem", str(min(6 * GIB, total_mem // 4)), allow_zero=True)
    cores = min(total_cores - reserve_cores, integer("max_cores", total_cores, 1))
    mem = min(total_mem - reserve_mem, size("max_mem", str(total_mem)))
    if cores <= 0 or mem <= 0:
        raise ValueError("Desktop reserves leave no capacity for agent-run")
    return Budget(cores, mem, total_cores - reserve_cores - load, available - reserve_mem,
                  integer("aging_seconds", 300, 1))


def ordered_waiters(waiting, now, aging):
    def priority(job):
        arrival = job["queued"]
        if now - arrival >= aging:
            return (0, arrival, job["id"])
        return (1, job["estimate"], arrival, job["id"])

    return sorted(waiting, key=priority)


def select(waiting, running, budget, now, has_slot):
    if not has_slot:
        return None
    # Reservations constrain total demand, while live counters protect against
    # desktop apps and jobs outside the queue. Neither is a hard OS quota.
    cores = min(budget.cores - sum(job["cores"] for job in running), budget.live_cores)
    mem = min(budget.mem - sum(job["mem"] for job in running), budget.live_mem)
    for job in waiting:
        if not budget.allows(job):
            continue
        if job["cores"] <= cores and job["mem"] <= mem:
            return job["id"]
        if now - job["queued"] >= budget.aging:
            # Drain instead of admitting more short jobs ahead of an aged head.
            break
    return None


def acquire(stream):
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def write_record(stream, job):
    stream.seek(0)
    stream.truncate()
    json.dump(job, stream)
    stream.flush()


def state_path():
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local/state"
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute to share admission across projects")
    return (base / "agent-run").resolve()


class Queue:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.waiting = root / "waiting"
        self.waiting.mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def gate(self):
        with (self.root / "admission.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def budget(self):
        return read_budget(self.root / "config.json")

    def waiters(self, *, prune=False):
        waiting = []
        for path in self.waiting.glob("*.json"):
            with path.open() as stream:
                if not acquire(stream):
                    waiting.append(json.load(stream))
                elif prune:
                    path.unlink()
        return waiting

    def slots(self):
        running, free = [], None
        # Keep the namespace fixed so every active slot stays visible.
        for index in range(SLOTS):
            path = self.root / f"slot-{index}.lock"
            try:
                stream = path.open()
            except FileNotFoundError:
                if free is None:
                    free = path
                continue
            with stream:
                if not acquire(stream):
                    running.append(json.load(stream))
                elif free is None:
                    free = path
        return running, free

    @contextmanager
    def admission(self, job, signals):
        lease = None
        with self.gate():
            self.check_size(job, self.budget())
            fd, name = tempfile.mkstemp(suffix=".json", dir=self.waiting)
            path = Path(name)
            waiter = os.fdopen(fd, "w")
            try:
                acquire(waiter)
                job.update(id=path.stem, queued=time.monotonic())
                write_record(waiter, job)
            except BaseException:
                waiter.close()
                path.unlink()
                raise

        try:
            notice = 0
            while not signals.pending:
                with self.gate():
                    now = time.monotonic()
                    budget = self.budget()
                    self.check_size(job, budget)
                    waiting = ordered_waiters(self.waiters(prune=True), now, budget.aging)
                    running, free = self.slots()
                    selected = select(waiting, running, budget, now, free is not None)
                    if selected == job["id"] and not signals.pending:
                        lease = free.open("a+")
                        if not acquire(lease):
                            raise RuntimeError("Slot unexpectedly changed under the admission gate")
                        job["started"] = now
                        write_record(lease, job)
                        path.unlink()
                        break
                    ahead = next(index for index, candidate in enumerate(waiting)
                                 if candidate["id"] == job["id"])
                if now >= notice:
                    print(f"agent-run: waiting for {resources(job['cores'], job['mem'])}; "
                          f"{ahead} {'job' if ahead == 1 else 'jobs'} ahead "
                          f"({now - job['queued']:.0f}s waited)", file=sys.stderr, flush=True)
                    notice = now + 15
                time.sleep(POLL_SECONDS)
            if lease is None:
                raise InterruptedError
            yield lease.fileno()
        finally:
            if lease is not None:
                lease.close()  # Never unlock an open-file description inherited by a child.
            with self.gate():
                path.unlink(missing_ok=True)
                waiter.close()

    @staticmethod
    def check_size(job, budget):
        if not budget.allows(job):
            raise ValueError(f"Request {resources(job['cores'], job['mem'])} exceeds machine budget "
                             f"of {resources(budget.cores, budget.mem)}")

    def status(self):
        with self.gate():
            budget = self.budget()
            now = time.monotonic()
            running, _ = self.slots()
            waiting = ordered_waiters(self.waiters(), now, budget.aging)
        print(f"Machine budget {resources(budget.cores, budget.mem)}  |  "
              f"Live headroom {max(0, budget.live_cores):.1f} cores / "
              f"{display_memory(max(0, budget.live_mem))}")
        print(f"{len(running)} running, {len(waiting)} waiting")
        for state, jobs, since in (("running", running, "started"), ("waiting", waiting, "queued")):
            for job in jobs:
                # Escape control characters before displaying caller-supplied text.
                label = json.dumps(job["label"], ensure_ascii=True)[1:-1]
                cwd = json.dumps(job["cwd"], ensure_ascii=True)[1:-1]
                print(f"{state} {label}  |  pid {job['pid']}  |  "
                      f"{resources(job['cores'], job['mem'])}  |  "
                      f"{now - job[since]:.1f}s  |  {cwd}")

    def record(self, job, code):
        event = dict(job, time=time.time(), exit_code=code,
                     wait_seconds=job["started"] - job["queued"],
                     hold_seconds=time.monotonic() - job["started"])
        with self.gate(), (self.root / "telemetry.jsonl").open("a+b") as log:
            if log.tell():
                log.seek(-1, os.SEEK_END)
                if log.read(1) != b"\n":
                    log.write(b"\n")  # Isolate a previous interrupted write.
            log.write((json.dumps(event) + "\n").encode("ascii"))

    def stats(self):
        count = failures = 0
        totals, maxima = [0., 0.], [0., 0.]
        try:
            with (self.root / "telemetry.jsonl").open() as log:
                for line in log:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # A killed writer can leave a partial record.
                    count += 1
                    failures += event["exit_code"] != 0
                    for i, name in enumerate(("wait_seconds", "hold_seconds")):
                        totals[i] += event[name]
                        maxima[i] = max(maxima[i], event[name])
        except FileNotFoundError:
            pass
        print(f"{count} completed, {failures} nonzero exits")
        if count:
            for label, total, maximum in zip(("wait", "hold"), totals, maxima):
                print(f"{label}: mean {total / count:.2f}s, max {maximum:.2f}s")


class Signals:
    def __init__(self):
        self.pending = None
        self.child = None

    @contextmanager
    def installed(self):
        previous = {sig: signal.signal(sig, self.handle) for sig in FORWARDED_SIGNALS}
        try:
            yield self
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def forward(self, signum):
        try:
            os.killpg(self.child.pid, signum)
        except ProcessLookupError:
            pass

    def handle(self, signum, _frame):
        if self.child is None:
            self.pending = self.pending or signum
        else:
            self.forward(signum)

    def run(self, command, lease, java_cores):
        if self.pending:
            return 128 + self.pending

        environment = dict(os.environ, **{LEASE_ENV: str(lease)})
        if java_cores is not None:
            options = environment.get("JAVA_TOOL_OPTIONS", "")
            environment["JAVA_TOOL_OPTIONS"] = f"{options} -XX:ActiveProcessorCount={java_cores}".strip()
        try:
            self.child = subprocess.Popen(command, env=environment, pass_fds=(lease,),
                                          start_new_session=True)
        except FileNotFoundError as error:
            print(f"agent-run: {error}", file=sys.stderr)
            return 127
        except PermissionError as error:
            print(f"agent-run: {error}", file=sys.stderr)
            return 126

        # A signal received while Popen was creating the group is still pending.
        if self.pending:
            self.forward(self.pending)
        code = self.child.wait()
        self.child = None
        return 128 - code if code < 0 else code


def defaults(command):
    name = Path(command[0]).name
    if name in ("mvn", "mvnw"):
        return 4, 4 * GIB, 600
    if name == "java" and "-jar" in command:
        jar = command.index("-jar")
        if command[jar + 2:jar + 4] == ["bench", "run"]:
            args = command[jar + 4:]
            cores = 4
            for i, token in enumerate(args):
                if token == "--jobs" and i + 1 < len(args):
                    cores = quantity(args[i + 1], {"": 1})
                elif token.startswith("--jobs="):
                    cores = quantity(token.split("=", 1)[1], {"": 1})
            return cores, 3 * GIB, 600
    return 4, 2 * GIB, 600


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, usage=(
        "%(prog)s [--mem 4G] [--cores 4] [--est 10m] [--label LABEL] [--java-cores] -- COMMAND ...\n"
        "       %(prog)s --status | --stats"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show running and waiting jobs")
    mode.add_argument("--stats", action="store_true", help="summarize completed-job telemetry")
    parser.add_argument("--mem", help="estimated memory, such as 4G")
    parser.add_argument("--cores", type=int, help="estimated CPU cores")
    parser.add_argument("--est", help="expected run time for queue ordering, such as 10m")
    parser.add_argument("--label", help="name to show in the queue")
    parser.add_argument("--java-cores", action="store_true", help="export a JVM processor-count hint")
    argv = sys.argv[1:] if argv is None else argv
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    observation = args.status or args.stats
    if observation and (split < len(argv) or args.java_cores or any(
            value is not None for value in (args.mem, args.cores, args.est, args.label))):
        parser.error("--status and --stats do not take a command or resource options")
    if not observation and not command:
        parser.error("supply a command after --")

    try:
        queue = Queue(state_path())
        if observation:
            queue.status() if args.status else queue.stats()
            return 0
        cores, mem, estimate = defaults(command)
        cores = cores if args.cores is None else args.cores
        mem = mem if args.mem is None else memory(args.mem)
        estimate = estimate if args.est is None else duration(args.est)
        if cores <= 0:
            raise ValueError("--cores must be positive")
        job = dict(cores=cores, mem=mem, estimate=estimate, pid=os.getpid(), cwd=os.getcwd(),
                   label=args.label or Path(command[0]).name)
        with Signals().installed() as signals:
            try:
                with queue.admission(job, signals) as lease:
                    code = signals.run(command, lease, cores if args.java_cores else None)
                    try:
                        queue.record(job, code)
                    except OSError as error:
                        print(f"agent-run: could not record telemetry: {error}", file=sys.stderr)
                    return code
            except InterruptedError:
                if signals.pending:
                    return 128 + signals.pending
                raise
    except (OSError, ValueError, KeyError) as error:
        print(f"agent-run: {error}", file=sys.stderr)
        return 2
