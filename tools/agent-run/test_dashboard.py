import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from agent_run import GIB
import agent_run_dashboard as dashboard
from test_agent_run import ENTRY, ROOT


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or "/var/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "agent-run"
        self.state.mkdir()
        (self.state / "waiting").mkdir()
        self.now = time.monotonic()
        self.source = dashboard.Snapshot(self.state)

    def record(self, path, data, *, locked=True, prefix=b""):
        path.write_bytes(prefix + json.dumps(data).encode())
        if locked:
            stream = path.open("rb")
            self.addCleanup(stream.close)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def job(self, identifier, **overrides):
        job = dict(id=identifier, queued=self.now - 10, pid=os.getpid(),
                   cores=1, mem=GIB, estimate=60, label=identifier, cwd="/example/repo")
        job.update(overrides)
        return job

    def test_snapshot_orders_waiters_and_explains_admission(self):
        self.record(self.state / "config.json", {"max_cores": 4, "aging_seconds": 5}, locked=False)
        self.record(self.state / "slot-0.lock",
                    self.job("older", started=self.now - 4, command="make test", cores=3))
        for name, job in {
            "aged": self.job("aged", cores=2),
            "short": self.job("short", estimate=2, queued=self.now - 1),
            "long": self.job("long", queued=self.now - 2),
        }.items():
            self.record(self.state / "waiting" / (name + ".json"), job)
        self.record(self.state / "slot-1.lock", self.job("stale"), locked=False)
        with patch("agent_run_dashboard.host_resources", return_value=(16, 32 * GIB, 24 * GIB, 1)):
            result = self.source()
        running, = result["running"]
        self.assertEqual(running["command"], "make test")
        self.assertEqual(running["cwd"], "/example/repo")
        self.assertEqual(running["pid"], os.getpid())
        self.assertGreaterEqual(running["held_seconds"], 4)
        self.assertLessEqual(running["remaining_seconds"], 56)
        self.assertTrue(running["owner_chain"])
        waiting = result["waiting"]
        self.assertEqual([job["id"] for job in waiting], ["aged", "short", "long"])
        self.assertEqual(waiting[0]["admission"], "Reserved CPU capacity")
        self.assertEqual(waiting[1]["admission"], "Draining for aged waiter")
        self.assertGreaterEqual(waiting[0]["wait_seconds"], 10)
        self.assertEqual(result["machine"]["reserve_mem"], 6 * GIB)

    def test_legacy_lease_and_telemetry_survive_partial_records(self):
        self.record(self.state / "slot-0.lock", self.job("legacy", started=self.now - 1))
        (self.state / "telemetry.jsonl").write_text(
            json.dumps(dict(time=100, label="done", wait_seconds=2.5, hold_seconds=9, exit_code=0))
            + '\n{"incomplete":\n')
        result = self.source()
        self.assertTrue(result["running"][0]["command"])
        event, = result["recent"]
        self.assertEqual((event["wait_seconds"], event["hold_seconds"]), (2.5, 9))

    def test_dashboard_reads_readonly_copy_and_leaves_absent_state_absent(self):
        self.record(self.state / "slot-0.lock", self.job("unlocked", started=self.now), locked=False)
        self.record(self.state / "slot-1.lock", self.job("active", started=self.now))
        paths = list(self.state.rglob("*")) + [self.state]
        for path in paths:
            self.addCleanup(path.chmod, path.stat().st_mode & 0o777)
            path.chmod(0o555 if path.is_dir() else 0o444)
        before = {path: (path.stat().st_mtime_ns, path.stat().st_mode) for path in paths}
        contents = {path: path.read_bytes() for path in paths if path.is_file()}
        with patch("fcntl.flock", side_effect=AssertionError("dashboard acquired a lock")):
            self.assertEqual([job["id"] for job in self.source()["running"]], ["active"])
            missing = self.root / "missing"
            self.assertEqual(dashboard.Snapshot(missing)()["waiting"], [])
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.state.rglob("*")) + [self.state], paths)
        self.assertEqual({path: (path.stat().st_mtime_ns, path.stat().st_mode) for path in paths}, before)
        self.assertEqual({path: path.read_bytes() for path in contents}, contents)

    def test_unmanaged_processes_exclude_running_process_trees(self):
        proc = self.root / "proc"
        proc.mkdir()

        def write_process(pid, name, parent, ticks, rss):
            directory = proc / str(pid)
            directory.mkdir(exist_ok=True)
            fields = ["0"] * 22
            fields[0], fields[1] = "S", str(parent)
            fields[11], fields[19], fields[21] = str(ticks), str(pid), str(rss)
            (directory / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))

        write_process(31, "agent-run", 1, 20, 100)
        write_process(32, "managed (worker)", 31, 20, 800)
        write_process(33, "unmanaged", 1, 20, 400)
        previous = dashboard.Processes(proc)
        write_process(32, "managed (worker)", 31, 220, 800)
        write_process(33, "unmanaged", 1, 80, 400)
        current = dashboard.Processes(proc)
        result = current.heavy(previous.rows, 2, {31})
        for ranking in ("cpu", "rss"):
            row, = result[ranking]
            self.assertEqual(row["pid"], 33)
            self.assertEqual(row["cpu_percent"], 60 / current.hz / 2 * 100)

    def test_cpu_time_uses_active_ticks_and_excludes_guest_double_counting(self):
        proc = self.root / "proc"
        proc.mkdir()
        stat_file = proc / "stat"
        stat_file.write_text("cpu 10 2 3 20 1 2 1 1 8 1\n")
        self.assertEqual(dashboard.cpu_times(proc), (19, 40))

    def test_maven_adapter_reads_common_git_dir_and_lock_prefixed_records(self):
        repo = self.root / "example"
        repo.mkdir()
        common = self.root / "shared.git"
        gitdir = common / "worktrees" / "example"
        gitdir.mkdir(parents=True)
        (repo / ".git").write_text("gitdir: ../shared.git/worktrees/example\n")
        (gitdir / "commondir").write_text("../..\n")
        (common / "maven-running").mkdir()
        (common / "maven-waiters").mkdir()
        self.record(self.state / "config.json", {"maven_repos": [str(repo)]}, locked=False)
        self.record(common / "maven-running" / ("a" * 32 + ".lease"),
                    dict(kind="verify", worktree="example", admitted=self.now - 2,
                         cpuCores=4, memoryBytes=2 * GIB), prefix=b"\0")
        self.record(common / "maven-waiters" / ("b" * 32 + ".request"),
                    dict(kind="test", enqueued=self.now - 1, submitted=self.now - 1, estimate=60,
                         admission=dict(reason="memory", updated=time.time())), prefix=b"\0")
        repo_data, = self.source()["maven"]
        self.assertEqual(repo_data["repo"], str(repo))
        self.assertEqual(repo_data["running"][0]["label"], "verify")
        self.assertEqual(repo_data["running"][0]["pid"], os.getpid())
        self.assertEqual(repo_data["waiting"][0]["admission"], "memory")

    def test_http_endpoints_and_loopback_host_check(self):
        server = dashboard.make_server(self.source, 0)
        with server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                host, port = server.server_address
                self.assertEqual(host, "127.0.0.1")
                url = f"http://{host}:{port}"
                with urlopen(url + "/", timeout=3) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    page = response.read().decode()
                    self.assertIn("Agent Run Dashboard", page)
                    self.assertIn("/api/status", page)
                with urlopen(url + "/api/status", timeout=3) as response:
                    self.assertEqual(response.headers.get_content_type(), "application/json")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn("machine", json.load(response))
                for route, headers, code in [
                    ("/missing", {}, 404), ("/api/status", {"Host": "other.invalid"}, 403)
                ]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(url + route, headers=headers), timeout=3)
                    with error.exception as response:
                        self.assertEqual(response.code, code)
            finally:
                server.shutdown()
                worker.join()

    def test_runner_publishes_display_command_without_arbitrary_arguments(self):
        (self.root / "host.json").write_text(json.dumps([16, 32 * GIB, 24 * GIB, 1]))
        result = subprocess.run(
            [sys.executable, "-c", ENTRY, str(ROOT), "--cores", "1", "--mem", "1M",
             "--label", "Synthetic job", "--", "/bin/true", "--token", "synthetic-secret"],
            env=dict(os.environ, XDG_STATE_HOME=str(self.root)),
            cwd=self.root, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lease = (self.state / "slot-0.lock").read_text()
        self.assertNotIn("synthetic-secret", lease)
        self.assertEqual(json.loads(lease)["command"], "true …")
        self.assertEqual(json.loads(lease)["label"], "Synthetic job")
        self.assertEqual(json.loads(lease)["cwd"], str(self.root))


if __name__ == "__main__":
    unittest.main()
