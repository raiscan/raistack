"""Read-only Linux snapshots for the local agent-run dashboard."""

from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from agent_run import (SLOTS, budget_for, display_command, headroom, host_resources,
                       ordered_waiters, select)

DEFAULT_PORT = 8767
REFRESH_SECONDS = 2
RECORD_BYTES = 64 * 1024
MAVEN_AGING_SECONDS = 300
MAVEN_REPORT_SECONDS = 15
MAVEN_REASONS = frozenset({
    "memory", "cpu", "slots", "exclusive", "worktree", "priority", "draining", "ready", "unknown",
})


def number(value, *, minimum=0):
    if type(value) in (int, float) and minimum <= value <= 2**63 - 1 and math.isfinite(value):
        return value
    return None


def text(value):
    if isinstance(value, str) and value:
        return "".join(c if c.isprintable() else " " for c in value[:1024])
    return None


def elapsed(now, then):
    return max(0, now - then) if then is not None else None


def identity(info):
    return os.major(info.st_dev), os.minor(info.st_dev), info.st_ino


def read_file(path, *, limit=RECORD_BYTES, tail=False):
    """Read bounded regular files and capture their inode without taking a lock."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Not a regular file")
        offset = max(0, info.st_size - limit) if tail else 0
        if offset:
            stream.seek(offset)
        data = stream.read(limit if tail else limit + 1)
        if len(data) > limit:
            raise ValueError("Record exceeds size limit")
        if offset:
            data = data.partition(b"\n")[2]
        return data, identity(info)


def object_record(data):
    record = json.loads(data)
    if not isinstance(record, dict):
        raise ValueError("Expected a JSON object")
    return record


def flock_entry(line):
    fields = line.split()
    if len(fields) < 8 or fields[1] != "FLOCK" or fields[3] != "WRITE":
        return None
    try:
        major, minor, inode = fields[5].split(":")
        return (int(major, 16), int(minor, 16), int(inode)), int(fields[4])
    except ValueError:
        return None


def cpu_times(proc):
    try:
        with (proc / "stat").open() as stream:
            fields = stream.readline().split()
        if len(fields) < 9 or fields[0] != "cpu":
            return None
        ticks = [int(field) for field in fields[1:9]]
        total = sum(ticks)
        return total - ticks[3] - ticks[4], total
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    parent: int
    start: int
    ticks: int
    rss: int


class Processes:
    def __init__(self, proc):
        self.root = proc
        self.rows = {}
        self.hz = os.sysconf("SC_CLK_TCK")
        page_size = os.sysconf("SC_PAGE_SIZE")
        for path in proc.iterdir():
            if not path.name.isascii() or not path.name.isdecimal():
                continue
            try:
                name, _, fields = (path / "stat").read_text(errors="replace").rpartition(")")
                fields = fields.split()
                pid = int(path.name)
                self.rows[pid] = Process(
                    pid, text(name.partition("(")[2]), int(fields[1]), int(fields[19]),
                    int(fields[11]) + int(fields[12]), max(0, int(fields[21])) * page_size,
                )
            except (OSError, ValueError, IndexError):
                continue

    def chain(self, pid):
        chain = []
        seen = set()
        while pid in self.rows and pid not in seen:
            process = self.rows[pid]
            chain.append(dict(pid=pid, name=process.name))
            seen.add(pid)
            pid = process.parent
        return chain

    def command(self, pid):
        if pid is None:
            return None
        try:
            with (self.root / str(pid) / "cmdline").open("rb") as stream:
                data = stream.read(8192)
        except OSError:
            return None
        argv = data.decode(errors="replace").rstrip("\0").split("\0") if data else []
        # The saved PID is normally the launcher. An inherited lease can
        # instead resolve to a child whose argv already starts with the job.
        if any(Path(arg).name in ("agent-run", "agent_run.py") for arg in argv[:2]):
            argv = argv[argv.index("--") + 1:] if "--" in argv else []
        return text(display_command(argv))

    def cwd(self, pid):
        if pid is None:
            return None
        try:
            return text(os.readlink(self.root / str(pid) / "cwd"))
        except OSError:
            return None

    def heavy(self, previous, interval, managed):
        # Propagate membership through child processes without reading argv or
        # the environment of unrelated jobs.
        children = {}
        for process in self.rows.values():
            children.setdefault(process.parent, []).append(process.pid)
        excluded = set(managed)
        pending = list(managed)
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in excluded:
                    excluded.add(child)
                    pending.append(child)
        rows = []
        for pid, process in self.rows.items():
            if pid in excluded or pid == os.getpid() or not process.rss:
                continue
            old = previous.get(pid)
            cpu = None
            if old and old.start == process.start and interval > 0:
                cpu = max(0, (process.ticks - old.ticks) / self.hz / interval * 100)
            rows.append(dict(pid=pid, name=process.name, rss=process.rss, cpu_percent=cpu))
        return dict(
            cpu=sorted((row for row in rows if row["cpu_percent"]),
                       key=lambda row: (-row["cpu_percent"], row["pid"]))[:8],
            rss=sorted(rows, key=lambda row: (-row["rss"], row["pid"]))[:8],
            cpu_sampled=bool(previous),
        )


class LockView:
    def __init__(self, proc, processes, warnings):
        self.proc = proc
        self.processes = processes
        self.by_inode = {}
        self.available = True
        self.by_pid = {}
        try:
            with (proc / "locks").open() as stream:
                for line in stream:
                    entry = flock_entry(line)
                    if entry:
                        key, pid = entry
                        self.by_inode.setdefault(key[2], []).append((key, pid))
        except OSError:
            warnings.append("Linux lock information is unavailable. Queue entries are unverified.")
            self.available = False

    def held_by(self, pid):
        if pid not in self.by_pid:
            files = set()
            try:
                for entry in (self.proc / str(pid) / "fdinfo").iterdir():
                    try:
                        with entry.open() as stream:
                            for line in stream:
                                lock = flock_entry(line[6:]) if line.startswith("lock:\t") else None
                                if lock:
                                    info = (entry.parent.parent / "fd" / entry.name).stat()
                                    files.add((identity(info), lock[0]))
                    except OSError:
                        continue
            except OSError:
                pass
            self.by_pid[pid] = files
        return self.by_pid[pid]

    def holder(self, key):
        """Return liveness and the best observable holder, even after launcher exit."""
        if not self.available:
            return "unverified", None
        candidates = self.by_inode.get(key[2], ())
        for alias, pid in candidates:
            if (key, alias) in self.held_by(pid):
                return "active", pid
        if candidates:
            # Linux can retain the original owner's PID after it exits, and
            # overlay mounts can report a different device in /proc/locks.
            for pid in self.processes.rows:
                if any((key, alias) in self.held_by(pid) for alias, _ in candidates):
                    return "active", pid
        if any(alias == key for alias, _ in candidates):
            return "active", None
        return ("unverified" if candidates else "stale"), None


def live_records(paths, lock_view, warnings, *, prefix=b""):
    for path in paths:
        try:
            data, key = read_file(path)
            liveness, pid = lock_view.holder(key)
            if liveness == "stale":
                continue
            if not data.startswith(prefix):
                raise ValueError("Unexpected record prefix")
            yield path, object_record(data[len(prefix):]), liveness, pid
        except FileNotFoundError:
            continue  # Queue records can disappear during a snapshot.
        except (OSError, ValueError, RecursionError):
            warnings.append("Some queue records were unreadable or incomplete.")
            yield path, None, "unverified", None


def agent_jobs(paths, locks, processes, warnings, now, *, running):
    rows = []
    complete = True
    for path, record, liveness, holder in live_records(paths, locks, warnings):
        complete = complete and liveness == "active"
        if record is None:
            continue
        queued = number(record.get("queued"))
        started = number(record.get("started"))
        cores = number(record.get("cores"), minimum=1)
        mem = number(record.get("mem"), minimum=1)
        estimate = number(record.get("estimate"), minimum=1)
        required = (cores, mem, estimate, queued, started) if running else (cores, mem, estimate, queued)
        if None in required:
            complete = False
            warnings.append("Some records lacked timing or reservation fields.")
            continue
        # Do not use an unverified PID to inspect an unrelated process.
        held = elapsed(now, started) if running else None
        rows.append(dict(
            id=path.stem if not running else text(record.get("id")) or path.stem,
            pid=holder or number(record.get("pid"), minimum=1),
            liveness=liveness, label=text(record.get("label")),
            cwd=text(record.get("cwd")) or processes.cwd(holder),
            command=text(record.get("command")) or processes.command(holder),
            owner_chain=processes.chain(holder),
            cores=cores, mem=mem, estimate=estimate, queued=queued,
            wait_seconds=elapsed(started if running else now, queued), held_seconds=held,
            remaining_seconds=max(0, estimate - held) if running else None,
        ))
    return rows, complete


def admission_reasons(waiting, running, budget, now, complete):
    has_slot = len(running) < SLOTS
    selected = select(waiting, running, budget, now, has_slot) if budget and complete else None
    cores, mem = headroom(running, budget) if budget else (0, 0)
    draining = False
    earlier_ready = False
    for position, job in enumerate(waiting, 1):
        if not budget or not complete:
            reason = "Admission unknown"
        elif not budget.allows(job):
            reason = "Exceeds machine budget"
        elif not has_slot:
            reason = "All lease slots occupied"
        elif earlier_ready:
            reason = "Earlier waiter can start"
        elif draining:
            reason = "Draining for aged waiter"
        elif selected == job["id"]:
            reason = "Ready in this snapshot"
            earlier_ready = True
        else:
            limits = []
            if job["cores"] > cores:
                limits.append("Live CPU load" if job["cores"] > budget.live_cores
                              else "Reserved CPU capacity")
            if job["mem"] > mem:
                limits.append("Live memory headroom" if job["mem"] > budget.live_mem
                              else "Reserved memory capacity")
            reason = " · ".join(limits)
            draining = now - job["queued"] >= budget.aging
        job.update(position=position, admission=reason)


def recent_jobs(state, warnings):
    try:
        data, _ = read_file(state / "telemetry.jsonl", limit=128 * 1024, tail=True)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        warnings.append("Recent completions could not be read.")
        return []
    recent = []
    for line in reversed(data.splitlines()):
        try:
            event = object_record(line)
        except (ValueError, RecursionError):
            continue
        waited = number(event.get("wait_seconds"))
        held = number(event.get("hold_seconds"))
        when = number(event.get("time"))
        code = event.get("exit_code")
        if None in (waited, held, when) or when > 8.64e12 or type(code) is not int:
            continue
        recent.append(dict(time=when, label=text(event.get("label")), cwd=text(event.get("cwd")),
                           wait_seconds=waited, hold_seconds=held, exit_code=code))
        if len(recent) == 20:
            break
    return recent


def git_common_dir(repo):
    git = repo / ".git"
    if not git.is_dir():
        data, _ = read_file(git, limit=4096)
        marker, separator, value = os.fsdecode(data).strip().partition(": ")
        if marker != "gitdir" or not separator or not value:
            raise ValueError("Invalid Git worktree pointer")
        git = (repo / value).resolve()
    try:
        data, _ = read_file(git / "commondir", limit=4096)
    except FileNotFoundError:
        return git
    return (git / os.fsdecode(data).strip()).resolve()


def read_maven(config, locks, warnings, now, wall_time):
    repositories = config.get("maven_repos", [])
    if not isinstance(repositories, list):
        warnings.append("maven_repos must be a list of absolute repository paths.")
        return []
    views = []
    seen = set()
    for path in repositories:
        if not isinstance(path, str) or not Path(path).is_absolute():
            warnings.append("Ignored a Maven repository entry without an absolute path.")
            continue
        try:
            common = git_common_dir(Path(path))
        except (OSError, ValueError, RuntimeError):
            warnings.append("A configured Maven repository could not be opened.")
            continue
        if common in seen:
            continue
        seen.add(common)
        view = dict(repo=text(path), running=[], waiting=[])
        for directory, suffix, kind in (
                ("maven-running", "lease", "running"), ("maven-waiters", "request", "waiting")):
            paths = (file for file in (common / directory).glob(f"*.{suffix}")
                     if re.fullmatch("[0-9a-f]{32}", file.stem))
            for file, record, liveness, holder in live_records(paths, locks, warnings, prefix=b"\0"):
                if record is None:
                    continue
                row = dict(id=file.stem, liveness=liveness, pid=holder, label=text(record.get("kind")),
                           worktree=text(record.get("worktree")))
                if kind == "running":
                    row.update(cores=number(record.get("cpuCores")), mem=number(record.get("memoryBytes")),
                               held_seconds=elapsed(now, number(record.get("admitted"))))
                else:
                    report = record.get("admission")
                    reason = report.get("reason") if isinstance(report, dict) else None
                    updated = number(report.get("updated")) if isinstance(report, dict) else None
                    current = updated is not None and 0 <= wall_time - updated <= MAVEN_REPORT_SECONDS
                    enqueued = number(record.get("enqueued"), minimum=-(2**63))
                    estimate = number(record.get("estimate"), minimum=1)
                    if enqueued is None or estimate is None:
                        continue
                    row.update(queued=enqueued, estimate=estimate,
                               wait_seconds=elapsed(now, number(record.get("submitted"))),
                               admission=reason if current and isinstance(reason, str)
                               and reason in MAVEN_REASONS else "Admission unknown")
                    reservation = record.get("reservation")
                    if isinstance(reservation, list) and len(reservation) == 2:
                        row.update(mem=number(reservation[0]), cores=number(reservation[1]))
                view[kind].append(row)
        view["waiting"] = ordered_waiters(view["waiting"], now, MAVEN_AGING_SECONDS)
        for position, row in enumerate(view["waiting"], 1):
            row["position"] = position
        views.append(view)
    return views


class Snapshot:
    def __init__(self, state, proc=Path("/proc")):
        self.state = state
        self.proc = proc
        self.lock = threading.Lock()  # In-memory only, to share samples between browser tabs.
        self.previous = {}
        self.previous_cpu = None
        self.sampled = float("-inf")
        self.current = None

    def __call__(self):
        with self.lock:
            now = time.monotonic()
            if now - self.sampled >= REFRESH_SECONDS:
                self.current = self.collect(now)
                self.sampled = now
            return self.current

    def collect(self, now):
        wall_time = time.time()
        warnings = []
        config_valid = True
        try:
            config = object_record(read_file(self.state / "config.json")[0])
        except FileNotFoundError:
            config = {}
        except (OSError, ValueError, RecursionError):
            warnings.append("The queue configuration could not be read.")
            config = {}
            config_valid = False
        machine = {}
        budget = None
        try:
            host = host_resources()
            total_cores, total_mem, available, load = host
            machine.update(total_cores=total_cores, total_mem=total_mem,
                           mem_available=available, mem_used=total_mem - available, load_1m=load)
            if config_valid:
                budget = budget_for(config, host)
                machine.update(asdict(budget))
        except (OSError, KeyError, ValueError):
            warnings.append("The current budget or host counters could not be read.")
        cpu = cpu_times(self.proc)
        machine["busy_cores"] = None
        if cpu and self.previous_cpu and "total_cores" in machine:
            active = cpu[0] - self.previous_cpu[0]
            total = cpu[1] - self.previous_cpu[1]
            if 0 <= active <= total and total:
                machine["busy_cores"] = machine["total_cores"] * active / total
        self.previous_cpu = cpu
        processes = Processes(self.proc)
        locks = LockView(self.proc, processes, warnings)
        running, complete_running = agent_jobs(
            (self.state / f"slot-{i}.lock" for i in range(SLOTS)), locks, processes, warnings, now, running=True)
        waiting, complete_waiting = agent_jobs(
            sorted((self.state / "waiting").glob("*.json")), locks, processes, warnings, now, running=False)
        waiting = ordered_waiters(waiting, now, budget.aging if budget else 300)
        admission_reasons(waiting, running, budget, now, locks.available and complete_running and complete_waiting)
        machine.update(reserved_cores=sum(job["cores"] for job in running if job["liveness"] == "active"),
                       reserved_mem=sum(job["mem"] for job in running if job["liveness"] == "active"))
        managed = {job["owner_chain"][0]["pid"] for job in running + waiting if job["owner_chain"]}
        heavy = processes.heavy(self.previous, now - self.sampled, managed)
        self.previous = processes.rows
        return dict(
            captured_at=wall_time, refresh_seconds=REFRESH_SECONDS, machine=machine,
            running=running, waiting=waiting, recent=recent_jobs(self.state, warnings),
            unmanaged=heavy, maven=read_maven(config, locks, warnings, now, wall_time),
            warnings=list(dict.fromkeys(warnings)),
        )


def make_server(source, port=DEFAULT_PORT):
    page = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def handle(self):
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                pass

        def do_GET(self):
            hosts = self.headers.get_all("Host", [])
            if hosts != [f"127.0.0.1:{self.server.server_port}"]:
                self.send_error(403, "Use the dashboard's loopback URL")
                return
            if self.path == "/":
                media, body = "text/html", page
            elif self.path == "/api/status":
                media = "application/json"
                body = json.dumps(source(), allow_nan=False).encode("utf-8")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", f"{media}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; "
                             "script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(state, port):
    with make_server(Snapshot(state), port) as server:
        print(f"Agent Run Dashboard: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
