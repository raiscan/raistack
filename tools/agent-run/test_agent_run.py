import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
# Stub only the live host counters so these jobs can test admission even during
# an unrelated build. All processes use the real CLI, state files and flock.
ENTRY = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
import agent_run
agent_run.host_resources = lambda: json.loads(
    (Path(os.environ['XDG_STATE_HOME']) / 'host.json').read_text())
sys.exit(agent_run.main())
"""

JOB = """
from pathlib import Path
import os, sys, time
name, root = sys.argv[1:]
root = Path(root)
(root / (name + '.pid')).write_text(str(os.getpid()))
while not (root / (name + '.stop')).exists():
    time.sleep(.02)
"""


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # These fixtures are tiny, and live out of the source checkout.
        scratch = os.environ.get("TMPDIR") or "/var/tmp"
        directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "agent-run"
        self.state.mkdir()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root))
        self.using_budget()
        self.host()

    def host(self, available=32 << 30, load=0):
        # Atomically publish the sample to the parallel runners.
        sample = self.root / "host.next"
        sample.write_text(json.dumps([32, 32 << 30, available, load]))
        sample.replace(self.root / "host.json")

    def using_budget(self, **overrides):
        config = dict(max_cores=4, max_mem="2G", **overrides)
        (self.state / "config.json").write_text(json.dumps(config))

    def command(self, *args):
        return [sys.executable, "-c", ENTRY, str(ROOT), *args]

    def cli(self, *args):
        return subprocess.run(self.command(*args), env=self.env,
                              capture_output=True, text=True, timeout=5)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if result := predicate():
                return result
            time.sleep(.025)
        self.fail("Timed out waiting for synthetic job")

    def job(self, name, cores, mem, est="10m", code=JOB, flags=()):
        # Distinct working directories represent independent projects.
        cwd = self.root / name
        cwd.mkdir()
        args = ("--cores", str(cores), "--mem", mem, "--est", est, "--label", name,
                *flags, "--", sys.executable, "-c", code, name, str(self.root))
        process = subprocess.Popen(self.command(*args), cwd=cwd, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(self.clean_job, name, process)
        return process

    def clean_job(self, name, process):
        self.stop_job(name)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def started(self, name):
        return self.wait_for(lambda: (self.root / (name + ".pid")).is_file())

    def stop_job(self, name):
        (self.root / (name + ".stop")).touch()

    def status_has(self, line):
        status = self.cli("--status")
        self.assertEqual(status.returncode, 0, status.stderr)
        return line in status.stdout

    def test_concurrent_admission_across_working_directories_and_memory_limit(self):
        first = self.job("first", 1, "1G")
        self.started("first")
        second = self.job("second", 1, "1G")
        self.started("second")
        third = self.job("third", 1, "1G")
        self.wait_for(lambda: self.status_has("waiting third"))
        self.assertFalse((self.root / "third.pid").exists())
        self.stop_job("first")
        self.assertEqual(first.wait(timeout=5), 0)
        self.started("third")
        self.stop_job("second")
        self.stop_job("third")
        self.assertEqual(second.wait(timeout=5), 0)
        self.assertEqual(third.wait(timeout=5), 0)
        self.assertIn("3 completed", self.cli("--stats").stdout)
        self.assertIn("waiting for 1 core / 1G", third.stderr.read())

    def test_core_limit_and_short_job_preference(self):
        first = self.job("first", 4, "1M")
        self.started("first")
        self.job("long", 4, "1M")
        self.wait_for(lambda: self.status_has("waiting long"))
        short = self.job("short", 4, "1M", "1s")
        self.wait_for(lambda: self.status_has("waiting short"))
        self.stop_job("first")
        self.started("short")
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertFalse((self.root / "long.pid").exists())
        self.stop_job("short")
        self.assertEqual(short.wait(timeout=5), 0)
        self.started("long")

    def test_aged_head_drains_before_small_waiter(self):
        self.using_budget(aging_seconds=1)
        first = self.job("first", 3, "1M")
        self.started("first")
        self.job("aged", 4, "1M")
        self.wait_for(lambda: self.status_has("waiting aged"))
        time.sleep(1.05)
        self.job("short", 1, "1M", "1s")
        self.wait_for(lambda: self.status_has("waiting short"))
        self.assertFalse((self.root / "short.pid").exists())
        self.stop_job("first")
        self.started("aged")
        self.assertEqual(first.wait(timeout=5), 0)

    def test_killed_launcher_keeps_child_lease_until_child_exits(self):
        first = self.job("first", 4, "1M")
        self.started("first")
        fd = os.pidfd_open(int((self.root / "first.pid").read_text()))
        self.addCleanup(os.close, fd)
        self.addCleanup(self.kill_child, fd)
        first.kill()
        self.assertEqual(first.wait(timeout=5), -signal.SIGKILL)
        self.assertTrue(self.status_has("running first"))
        self.job("next", 4, "1M")
        self.wait_for(lambda: self.status_has("waiting next"))
        self.kill_child(fd)
        self.started("next")

    @staticmethod
    def kill_child(fd):
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_killed_waiter_cannot_block_queue(self):
        first = self.job("first", 4, "1M")
        self.started("first")
        waiter = self.job("dead", 4, "1M", "1s")
        self.wait_for(lambda: self.status_has("waiting dead"))
        waiter.kill()
        self.assertEqual(waiter.wait(timeout=5), -signal.SIGKILL)
        self.assertFalse(self.status_has("waiting dead"))
        self.job("next", 4, "1M")
        self.stop_job("first")
        self.started("next")

    def test_signal_forwarding_and_child_exit_code(self):
        code = """
from pathlib import Path
import os, signal, sys
name, root = sys.argv[1:]
def received(*_):
    sys.exit(47)
signal.signal(signal.SIGTERM, received)
(Path(root) / (name + '.pid')).write_text(str(os.getpid()))
while True:
    signal.pause()
"""
        process = self.job("signal", 4, "1M", code=code)
        self.started("signal")
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 47)
        result = self.cli("--", sys.executable, "-c", "raise SystemExit(37)")
        self.assertEqual(result.returncode, 37, result.stderr)

    def test_java_hint_is_opt_in_and_preserves_options(self):
        self.env["JAVA_TOOL_OPTIONS"] = "-Dexample=kept"
        command = ("--", sys.executable, "-c", "import os; print(os.environ['JAVA_TOOL_OPTIONS'])")
        self.assertEqual(self.cli(*command).stdout.strip(), "-Dexample=kept")
        self.assertEqual(self.cli("--cores", "2", "--java-cores", *command).stdout.strip(),
                         "-Dexample=kept -XX:ActiveProcessorCount=2")

    def test_live_memory_and_load_gate_even_when_budget_is_empty(self):
        self.host(available=6 << 30)
        job = self.job("live", 1, "1M")
        self.wait_for(lambda: self.status_has("waiting live"))
        self.host(load=30)
        time.sleep(.35)
        self.assertFalse((self.root / "live.pid").exists())
        self.host()
        self.started("live")
        self.stop_job("live")
        self.assertEqual(job.wait(timeout=5), 0)

    def test_defaults_for_maven_and_bench(self):
        from agent_run import defaults

        self.assertEqual((4, 4 << 30, 600), defaults(["./mvnw", "test"]))
        for worker_args in (["--jobs", "2"], ["--jobs=2"]):
            command = ["java", "-jar", "tool.jar", "bench", "run", *worker_args]
            self.assertEqual((2, 3 << 30, 600), defaults(command))

    def test_oversized_request_fails_without_waiting(self):
        result = self.cli("--cores", "5", "--", sys.executable, "-c", "raise SystemExit(99)")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("exceeds machine budget", result.stderr)


if __name__ == "__main__":
    unittest.main()
