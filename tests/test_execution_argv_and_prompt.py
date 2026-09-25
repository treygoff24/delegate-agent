import contextlib
import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from delegate_agent import (
    argv_builders,
    cli,
    cli_parser,
    describe_payload,
    errors,
    prompt_transport,
    record_io,
    request_build,
    request_models,
    run_registry,
    runner,
)
from delegate_agent import (
    config as delegate_config,
)
from tests.execution_test_base import (
    GIT_TEST_IDENTITY,
    MODULE_PATH,
    SCRIPT_PATH,
    ExecutionTestBase,
    make_git_repo,
    safe_temp_dirs,
)

# A recorded run child that reports its own TMPDIR integrity: it writes once,
# sleeps, and writes again, exiting 3 when the second write fails because its
# directory was removed underneath it. `sys.argv[1]` is the sleep.
_RECORDED_CHILD_SCRIPT = """\
import os
import pathlib
import sys
import time

temp = pathlib.Path(os.environ["TMPDIR"])
temp.joinpath("child-write-1.txt").write_text("first", encoding="utf-8")
time.sleep(float(sys.argv[1]))
try:
    temp.joinpath("child-write-2.txt").write_text("second", encoding="utf-8")
except OSError:
    raise SystemExit(3)
"""

_RECORDED_CHILD_RUN_ID = "del_20260922T175330Z_abcdef"

# The fixture's producer stands in for the test-owned Delegate process that
# drives a run: it waits on its stdin, so the test's own `communicate()` is
# positive evidence that the producer process exited, the same evidence a test
# holds after the Delegate subprocess it spawned returns.
_RECORDED_PRODUCER_SCRIPT = """\
import sys

sys.stdin.read()
"""


@dataclasses.dataclass
class RecordedChild:
    """A real attempt child in its own session, the record that names it, and the
    producer process that owns both.

    The producer is deliberately a different process from the attempt child: the
    deletion boundary under test is the producer's exit, because `runner` reuses
    one run's compact temp for every attempt of that run and only the producer
    decides whether another attempt follows.
    """

    registry_root: Path
    temp_path: Path
    pgid: int
    process: subprocess.Popen
    producer: subprocess.Popen
    script: Path

    def wait_for_first_write(self, timeout: float = 10.0) -> None:
        """Block until the attempt child has written once inside its TMPDIR."""
        marker = self.temp_path / "child-write-1.txt"
        deadline = time.monotonic() + timeout
        while not marker.is_file():
            if time.monotonic() >= deadline:
                raise AssertionError(f"the recorded child never wrote inside {self.temp_path}")
            time.sleep(0.02)

    def producers(self) -> list[object]:
        """The producer evidence containment is handed for this fixture's record."""
        return [self.producer]

    def finish_producer(self) -> None:
        """Close the producer's stdin and reap it: positive completion evidence.

        Only a producer whose own process has exited can no longer launch an
        attempt into this run's temp.
        """
        self.producer.communicate(timeout=15)
        if self.producer.poll() is None:
            raise AssertionError("the fixture producer did not exit")


class ExecutionArgvAndPromptTests(ExecutionTestBase):
    def _announcement_config(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        return config

    def test_subprocess_tracked_runs_leave_no_production_compact_temp(self):
        """A real CLI child must not orphan the compact temp it allocated.

        The suite pins `run_scratch.PERSISTENT_TEMP_ROOT` in-process, but a
        child is a fresh interpreter that resolves the production
        `/var/tmp/dlg-<uid>` root, and only `runs prune` removes what it
        allocated: before this containment existed, every subprocess tracked run
        retained one directory there forever (observed 2026-09-22: 235 retained
        directories from previous gates). Both outcomes allocate before the
        child reports anything, so success and failure are both checked.
        """
        from tests import (
            assert_compact_temps_contained,
            compact_temp_names,
            derived_production_compact_temp,
            recorded_compact_temps,
        )

        for fake_exit in ("0", "1"):
            with self.subTest(fake_exit=fake_exit):
                repo = make_git_repo()
                self.addCleanup(repo.cleanup)
                fake_bin = self.make_cursor_safe_fake_agent()
                config = Path(repo.name) / "config.json"
                config.write_text(json.dumps(delegate_config.embedded_default_config()))
                env = os.environ.copy()
                env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
                env["DELEGATE_CONFIG"] = str(config)
                env["FAKE_EXIT"] = fake_exit
                base = self.private_tmp_env(env)

                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT_PATH),
                        "--cwd",
                        repo.name,
                        "--json",
                        "cursor",
                        "safe",
                        "review",
                    ],
                    text=True,
                    capture_output=True,
                    env=env,
                    check=False,
                )

                self.assertEqual(completed.returncode, int(fake_exit), completed.stderr)
                registry_root = Path(repo.name) / ".delegate"
                recorded = recorded_compact_temps(registry_root)
                self.assertEqual(len(recorded), 1, completed.stderr)
                run_id, temp_path = recorded[0]
                # The record and an independent derivation of the same run must
                # agree on the directory, which is also what `runs prune` checks
                # before deleting one.
                self.assertEqual(temp_path, derived_production_compact_temp(registry_root, run_id))
                self.assertTrue(temp_path.is_dir(), temp_path)

                assert_compact_temps_contained(registry_root, producers=[completed])

                self.assertFalse(temp_path.exists(), temp_path)
                self.assertNotIn(temp_path.name, compact_temp_names())
                self.assertEqual(safe_temp_dirs(base), set())

    def _spawn_recorded_child(
        self, *, delay: float, marker_in_argv: bool, publish_group: bool = True
    ) -> RecordedChild:
        """A real attempt child in its own session plus the record that names it.

        The manifest is the shape `runner._prepare_tracked_run` writes: the run's
        execution cwd, its compact child temp, and the pgid of the child it
        launched in a new session. The compact temp is named by the same
        deterministic derivation production uses, independently of the record.
        `marker_in_argv` decides whether the child's command line carries the
        recorded cwd, which is the only thing that authorizes a signal; without
        it the manifest names a live child that no command line proves, the
        shape a Pi launch has. `publish_group` decides whether the manifest
        carries the group at all: the producer records `tempPath` before the
        launch and adds pid/pgid only after `Popen` returns, so ``False`` is that
        prepublication record -- a live child no field of the record names.

        The fixture also spawns the producer that owns this record, so callers
        can hand containment a live producer (retention) or its reap (the
        boundary that permits deletion); see `publish_retry_child` for the
        attempt that producer would publish after a retry.
        """
        from tests import (
            PRODUCTION_COMPACT_TEMP_ROOT,
            derived_production_compact_temp,
            proc_harness,
        )

        registry_root = Path(tempfile.mkdtemp(prefix="delegate-recorded-registry-"))
        self.addCleanup(shutil.rmtree, registry_root, True)
        temp_path = derived_production_compact_temp(registry_root, _RECORDED_CHILD_RUN_ID)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, temp_path, True)
        child_dir = Path(tempfile.mkdtemp(prefix="delegate-recorded-child-"))
        self.addCleanup(shutil.rmtree, child_dir, True)
        script = child_dir / "child.py"
        script.write_text(_RECORDED_CHILD_SCRIPT, encoding="utf-8")
        producer_script = child_dir / "producer.py"
        producer_script.write_text(_RECORDED_PRODUCER_SCRIPT, encoding="utf-8")
        producer = subprocess.Popen(
            [sys.executable, str(producer_script)],
            stdin=subprocess.PIPE,
            text=True,
        )

        def finish_producer() -> None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                producer.communicate(timeout=15)
            if producer.poll() is None:
                producer.kill()
                producer.communicate()

        self.addCleanup(finish_producer)

        argv = [sys.executable, str(script), str(delay)]
        if marker_in_argv:
            argv.append(str(registry_root))
        env = os.environ.copy()
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(temp_path)
        process = subprocess.Popen(argv, env=env, start_new_session=True)
        pgid = os.getpgid(process.pid)

        def reap() -> None:
            with contextlib.suppress(OSError):
                proc_harness.reap_process_group(pgid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=15)

        self.addCleanup(reap)
        run_dir = registry_root / record_io.RUNS_DIR_NAME / _RECORDED_CHILD_RUN_ID
        run_dir.mkdir(parents=True)
        record = {
            "runId": _RECORDED_CHILD_RUN_ID,
            "executionCwd": str(registry_root),
            "tempPath": str(temp_path),
        }
        if publish_group:
            record["pgid"] = pgid
        (run_dir / record_io.MANIFEST_FILE).write_text(
            json.dumps(record),
            encoding="utf-8",
        )
        self.assertEqual(
            temp_path.parent,
            PRODUCTION_COMPACT_TEMP_ROOT,
            "the child fixture must own a real production compact temp",
        )
        return RecordedChild(registry_root, temp_path, pgid, process, producer, script)

    def publish_retry_child(self, child: RecordedChild, *, delay: float) -> subprocess.Popen:
        """Launch this run's retry attempt and republish it into the same record.

        Mirrors `runner._run_single_tracked_attempt`: the retry is a real child
        in its own session launched with the run's own compact temp as its
        TMPDIR, and its pid/pgid replace the previous attempt's in the same
        manifest -- written under the run's registry lock, which is the same
        lock containment takes for its deletion decision. The record is left as
        the current generation: the retried run, not the attempt that preceded
        it.
        """
        from tests import proc_harness

        env = os.environ.copy()
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(child.temp_path)
        process = subprocess.Popen(
            [sys.executable, str(child.script), str(delay)],
            env=env,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)

        def reap() -> None:
            with contextlib.suppress(OSError):
                proc_harness.reap_process_group(pgid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=15)

        self.addCleanup(reap)
        manifest_path = (
            child.registry_root
            / record_io.RUNS_DIR_NAME
            / _RECORDED_CHILD_RUN_ID
            / record_io.MANIFEST_FILE
        )
        with run_registry.registry_lock(child.registry_root, timeout_seconds=5.0):
            record = json.loads(manifest_path.read_text(encoding="utf-8"))
            record["pid"] = process.pid
            record["pgid"] = pgid
            run_registry.write_json_atomic(manifest_path, record)
        return process

    def test_containment_retains_the_temp_of_a_live_child_with_no_argv_marker(self):
        """A live child whose command line proves nothing keeps its TMPDIR.

        Pi takes its prompt on stdin and its normal argv carries no workspace
        flag, so a run manifest can name a live child that none of the paths in
        that record appear in. Containment used to filter exactly those children
        out and then remove the directory they were writing into, while still
        reporting the removal as a success. The child here exits 3 when a write
        fails, so it reports that outcome itself, and only a child whose exit is
        confirmed gone lets its temp go.

        Its producer runs the whole time, so the first retention is the producer
        boundary; the second, taken after that producer is reaped while the
        unmarked child is still writing, is the marker rule: the child's group is
        awaited, never signalled, because nothing in its command line proves the
        group is this run's.
        """
        from tests import assert_compact_temps_contained, reap_recorded_compact_temps

        with mock.patch("tests.COMPACT_TEMP_CHILD_GRACE_SECONDS", 0.4):
            child = self._spawn_recorded_child(delay=1.2, marker_in_argv=False)
            child.wait_for_first_write()

            # The producer is still alive: it may launch another attempt into
            # this same temp, so nothing is deleted.
            removed, surviving = reap_recorded_compact_temps(
                child.registry_root, producers=child.producers()
            )
            self.assertEqual(removed, [])
            self.assertEqual(surviving, [child.temp_path])
            self.assertTrue(child.temp_path.is_dir(), child.temp_path)

            child.finish_producer()
            with self.assertRaises(AssertionError) as caught:
                assert_compact_temps_contained(child.registry_root, producers=child.producers())

            self.assertIn(str(child.temp_path), str(caught.exception))
            self.assertTrue(child.temp_path.is_dir(), child.temp_path)
            self.assertEqual(child.process.wait(timeout=15), 0, "the child lost its TMPDIR")
            self.assertTrue((child.temp_path / "child-write-2.txt").is_file())

            # Once that child is gone the same call removes the temp it kept.
            removed, surviving = reap_recorded_compact_temps(
                child.registry_root, producers=child.producers()
            )
            self.assertEqual(surviving, [])
            self.assertEqual(removed, [child.temp_path])
            self.assertFalse(child.temp_path.exists(), child.temp_path)

    def test_containment_returns_a_live_producers_temp_until_that_process_exits(self):
        """A producer that may still retry keeps every temp of its run.

        The real Delegate subprocess is still running the fake engine here, and
        the fake engine writes one marker, waits, then writes a second one --
        exiting 3 when either write fails, so the child reports for itself
        whether teardown pulled its TMPDIR away. Containment called in that
        window must hand the exact temp back and name it: the run's compact temp
        is shared by every attempt the producer may still launch, so no group it
        happens to have launched is a deletion boundary. Only its exit is, and
        the same call removes the temp afterwards.
        """
        from tests import (
            compact_temp_names,
            derived_production_compact_temp,
            reap_recorded_compact_temps,
            recorded_compact_temps,
        )

        for fake_exit in ("0", "1"):
            with self.subTest(fake_exit=fake_exit):
                repo = make_git_repo()
                self.addCleanup(repo.cleanup)
                fake_bin = self.make_cursor_safe_fake_agent_writing_in_its_tmpdir()
                config = Path(repo.name) / "config.json"
                config.write_text(json.dumps(delegate_config.embedded_default_config()))
                env = os.environ.copy()
                env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
                env["DELEGATE_CONFIG"] = str(config)
                env["FAKE_EXIT"] = fake_exit
                env["FAKE_CHILD_DELAY"] = "0.5"
                base = self.private_tmp_env(env)

                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT_PATH),
                        "--cwd",
                        repo.name,
                        "--json",
                        "cursor",
                        "safe",
                        "review",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    registry_root = Path(repo.name) / ".delegate"
                    # The manifest records the path while the child runs, so
                    # the test can name the directory the child is writing into
                    # without waiting for the run to finish; the derivation
                    # check keeps that record honest.
                    deadline = time.monotonic() + 10
                    while True:
                        recorded = recorded_compact_temps(registry_root)
                        if recorded and (Path(recorded[0][1]) / "child-write-1.txt").is_file():
                            run_id, temp_path = recorded[0]
                            break
                        if time.monotonic() >= deadline:
                            self.fail("the tracked child never wrote inside its compact temp")
                        time.sleep(0.02)
                    self.assertEqual(
                        temp_path,
                        derived_production_compact_temp(registry_root, run_id),
                    )
                    # The producer is mid-flight: it may still launch a retry
                    # into this same temp, so containment hands it back.
                    removed, surviving = reap_recorded_compact_temps(
                        registry_root, producers=[process]
                    )
                    self.assertEqual(removed, [])
                    self.assertEqual(surviving, [temp_path])
                    self.assertTrue(temp_path.is_dir(), temp_path)
                    _stdout, stderr = process.communicate(timeout=15)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

                self.assertEqual(process.returncode, int(fake_exit), stderr)
                # The producer has exited, so the run's own child group is the
                # last thing settled before this temp may go.
                removed, surviving = reap_recorded_compact_temps(registry_root, producers=[process])
                self.assertEqual(surviving, [])
                self.assertEqual(removed, [temp_path])
                self.assertFalse(temp_path.exists(), temp_path)
                self.assertNotIn(temp_path.name, compact_temp_names())
                self.assertEqual(safe_temp_dirs(base), set())

    def test_containment_retains_the_temp_of_a_live_child_before_its_group_is_published(self):
        """A record that names no child group must not authorize deleting a temp.

        `runner._prepare_tracked_run` writes the compact ``tempPath`` before the
        run launches, and the child's pid/pgid reach that same manifest only
        afterwards, under the registry lock the runner holds across the launch
        (`runner._run_single_tracked_attempt`). Containment reads manifests
        without that lock, so ``tempPath`` with no group is a real intermediate
        state of a live child -- not a record of a run that never launched, as
        the containment previously assumed when it deleted the directory and
        reported success. The child here exits 3 when a write fails, so it
        reports that outcome itself.
        """
        from tests import assert_compact_temps_contained, reap_recorded_compact_temps

        child = self._spawn_recorded_child(delay=1.2, marker_in_argv=True, publish_group=False)
        child.wait_for_first_write()

        # A live producer retains the temp whatever its record says about the
        # attempt it has launched so far.
        removed, surviving = reap_recorded_compact_temps(
            child.registry_root, producers=child.producers()
        )
        self.assertEqual(removed, [])
        self.assertEqual(surviving, [child.temp_path])

        child.finish_producer()
        with self.assertRaises(AssertionError) as caught:
            assert_compact_temps_contained(child.registry_root, producers=child.producers())

        self.assertIn(str(child.temp_path), str(caught.exception))
        self.assertTrue(child.temp_path.is_dir(), child.temp_path)
        self.assertEqual(child.process.wait(timeout=15), 0, "the child lost its TMPDIR")
        self.assertTrue((child.temp_path / "child-write-2.txt").is_file())

        # The record still names no group, so the temp stays retained even now
        # that this fixture's producer and child have exited: a group the record
        # never published is unknown child state, and only a record that names a
        # group can establish that the directory has no live writer left.
        removed, surviving = reap_recorded_compact_temps(
            child.registry_root, producers=child.producers()
        )
        self.assertEqual(removed, [])
        self.assertEqual(surviving, [child.temp_path])
        self.assertTrue(child.temp_path.is_dir(), child.temp_path)

    def test_containment_does_not_read_an_unconfirmed_reap_as_a_child_exit(self):
        """A reap that does not settle its group must not authorize deletion.

        The reaper suppresses signal failures and returns nothing, so after the
        grace a signal is only a request: delivery, a delayed child, and an
        unsignallable member all reach the caller the same way. This child's
        command line does carry the recorded marker and the reap is replaced by
        a no-op, so containment -- with the producer already reaped, so the
        retention can only come from the unsettled group -- must keep the temp
        and report it while the child keeps writing; a real reap of the same
        shape removes the temp only after the group is confirmed gone.
        """
        from tests import assert_compact_temps_contained, reap_recorded_compact_temps

        with mock.patch("tests.COMPACT_TEMP_CHILD_GRACE_SECONDS", 0.4):
            with mock.patch(
                "tests.proc_harness.reap_recorded_group_matching", lambda pgid, marker: None
            ):
                child = self._spawn_recorded_child(delay=1.2, marker_in_argv=True)
                child.wait_for_first_write()
                child.finish_producer()

                with self.assertRaises(AssertionError) as caught:
                    assert_compact_temps_contained(child.registry_root, producers=child.producers())

                self.assertIn(str(child.temp_path), str(caught.exception))
                self.assertTrue(child.temp_path.is_dir(), child.temp_path)
                self.assertEqual(child.process.wait(timeout=15), 0, "the child lost its TMPDIR")

            signalled = self._spawn_recorded_child(delay=30, marker_in_argv=True)
            signalled.wait_for_first_write()
            signalled.finish_producer()

            removed, surviving = reap_recorded_compact_temps(
                signalled.registry_root, producers=signalled.producers()
            )

            self.assertEqual(surviving, [])
            self.assertEqual(removed, [signalled.temp_path])
            self.assertFalse(signalled.temp_path.exists(), signalled.temp_path)
            self.assertLess(
                signalled.process.wait(timeout=15),
                0,
                "the owned child was signalled, not waited out",
            )

    def test_containment_keeps_the_temp_of_the_generation_a_same_run_retry_published(self):
        """A group that went quiet is not authority to delete a run's temp.

        `runner._run_single_tracked_attempt` reuses one run's compact temp for
        the primary attempt, a thread retry, an auth fallback, and an
        empty-success retry: each attempt is a new child in a new session whose
        pid/pgid is published into the same manifest under the registry lock.
        Containment that mapped each manifest to the group it snapshotted and
        then used that map for the removal could confirm the old attempt quiet,
        delete the shared temp, and report success while the run's live retry was
        writing into it.

        This fixture publishes exactly that retry -- a real child, a real
        manifest write under the same lock -- once the old group has been
        confirmed quiet and before the deletion decision, which is the window the
        producer boundary cannot see on its own: the retry child is not a new
        process the producer launched, it *is* the run's next attempt. The retry
        child exits 3 when a write of its own fails, so it reports whether its
        TMPDIR survived.
        """
        from tests import proc_harness, reap_recorded_compact_temps

        child = self._spawn_recorded_child(delay=0.0, marker_in_argv=True)
        child.wait_for_first_write()
        self.assertEqual(child.process.wait(timeout=15), 0, "the first attempt failed")
        child.finish_producer()

        retry: dict[str, subprocess.Popen] = {}
        await_process_group = proc_harness.await_process_group

        def await_then_republish(pgid: int, *, timeout: float) -> bool:
            quiet = await_process_group(pgid, timeout=timeout)
            if pgid == child.pgid and "process" not in retry:
                retry["process"] = self.publish_retry_child(child, delay=1.2)
            return quiet

        with mock.patch("tests.proc_harness.await_process_group", await_then_republish):
            removed, surviving = reap_recorded_compact_temps(
                child.registry_root, producers=child.producers()
            )

        self.assertEqual(removed, [], "the temp went with the generation that was awaited")
        self.assertEqual(surviving, [child.temp_path])
        self.assertTrue(child.temp_path.is_dir(), child.temp_path)
        process = retry["process"]
        self.assertEqual(process.wait(timeout=15), 0, "the retry child lost its TMPDIR")
        self.assertTrue((child.temp_path / "child-write-2.txt").is_file())

        # The record now names the retry's group, and that is the generation to
        # settle: once it is gone the same call removes the temp it kept, so the
        # retention above was a handback rather than a leak.
        removed, surviving = reap_recorded_compact_temps(
            child.registry_root, producers=child.producers()
        )
        self.assertEqual(surviving, [])
        self.assertEqual(removed, [child.temp_path])
        self.assertFalse(child.temp_path.exists(), child.temp_path)

    def test_containment_deletes_nothing_without_producer_evidence(self):
        """A caller that proves no producer exited gets retention, not a delete.

        Containment cannot read producer completion out of the record: the
        manifest names the attempt child's pid/pgid, never the Delegate process
        that owns the run, and a temp whose owning process is unknown may be
        written into again by that process's next attempt. So a call with no
        usable evidence keeps every recorded temp and names it, even when the
        attempt child that the record names has already exited.
        """
        from tests import reap_recorded_compact_temps

        child = self._spawn_recorded_child(delay=0.0, marker_in_argv=True)
        child.wait_for_first_write()
        self.assertEqual(child.process.wait(timeout=15), 0, "the first attempt failed")

        removed, surviving = reap_recorded_compact_temps(child.registry_root)

        self.assertEqual(removed, [])
        self.assertEqual(surviving, [child.temp_path])
        self.assertTrue(child.temp_path.is_dir(), child.temp_path)

    def test_containment_keeps_every_temp_when_the_owned_process_scan_fails(self):
        """A scan that could not be completed is not evidence a producer exited.

        The scan answers "no owned Delegate process is left", and containment
        reads that as permission to delete a run's temp. `ps` failing, timing
        out, or printing a listing that cannot be read is no answer at all: it
        used to come back as an empty result and pass for a quiet machine, which
        certified a live producer gone. Here the recorded child has exited and
        its producer has been reaped, so deletion is legal on every other term --
        only the failed scan stands in the way, and it must stand in the way
        loudly while the temp stays put.
        """
        from tests import proc_harness, process_guard, reap_recorded_compact_temps
        from tests.process_guard import OwnedProcessScanError

        child = self._spawn_recorded_child(delay=0.0, marker_in_argv=True)
        child.wait_for_first_write()
        self.assertEqual(child.process.wait(timeout=15), 0, "the fixture child failed")
        child.finish_producer()

        with (
            mock.patch.object(
                process_guard.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["ps"], returncode=1, stdout="", stderr="ps: cannot read\n"
                ),
            ),
            self.assertRaises(OwnedProcessScanError),
        ):
            reap_recorded_compact_temps(
                child.registry_root,
                producers=[proc_harness.reaped_owned_producers(child.registry_root)],
            )

        self.assertTrue(child.temp_path.is_dir(), child.temp_path)

    def test_human_launch_announces_workspace_origin_before_execution(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = self._announcement_config()

        for args, expected_origin, cwd in (
            (["--cwd", repo.name, "droid", "work", "--model", "reviewer", "hello"], "--cwd", None),
            (["droid", "work", "--model", "reviewer", "hello"], "cwd", repo.name),
        ):
            with self.subTest(expected_origin=expected_origin):
                stdout = io.StringIO()
                stderr = io.StringIO()
                observed: dict[str, object] = {}

                def fake_execute_request(
                    request,
                    _json_mode,
                    _observed=observed,
                    _stdout=stdout,
                    **_kwargs,
                ):
                    _observed["request"] = request
                    _observed["stdout"] = _stdout.getvalue()
                    return 0, None

                with (
                    mock.patch.dict(os.environ, {"AI_PROFILE": ""}, clear=False),
                    mock.patch.object(request_build, "load_config", return_value=(config, "test")),
                    mock.patch.object(cli, "execute_request", side_effect=fake_execute_request),
                ):
                    if cwd is None:
                        code = cli.main(args, stdout=stdout, stderr=stderr)
                    else:
                        with contextlib.chdir(cwd):
                            code = cli.main(args, stdout=stdout, stderr=stderr)

                self.assertEqual(code, 0)
                expected_line = (
                    f"workspace: {Path(repo.name).resolve()} (git, from {expected_origin})\n"
                )
                self.assertEqual(stdout.getvalue(), expected_line)
                self.assertEqual(observed["stdout"], expected_line)
                request = observed["request"]
                self.assertIsInstance(request, request_models.Request)
                self.assertNotIn(
                    cli.INFERRED_NON_GIT_WORKSPACE_WARNING,
                    request.warnings,
                )

    def test_inferred_non_git_workspace_warns_without_refusing(self):
        config = self._announcement_config()
        with tempfile.TemporaryDirectory() as workspace:
            stdout = io.StringIO()
            stderr = io.StringIO()
            observed: dict[str, object] = {}

            def fake_execute_request(request, _json_mode, **_kwargs):
                observed["request"] = request
                return 0, None

            with (
                contextlib.chdir(workspace),
                mock.patch.dict(os.environ, {"AI_PROFILE": ""}, clear=False),
                mock.patch.object(request_build, "load_config", return_value=(config, "test")),
                mock.patch.object(cli, "execute_request", side_effect=fake_execute_request),
            ):
                code = cli.main(
                    ["droid", "work", "--model", "reviewer", "hello"],
                    stdout=stdout,
                    stderr=stderr,
                )

        self.assertEqual(code, 0)
        self.assertIn(
            f"workspace: {Path(workspace).resolve()} (directory, from cwd)\n",
            stdout.getvalue(),
        )
        request = observed["request"]
        self.assertIsInstance(request, request_models.Request)
        self.assertIn(cli.INFERRED_NON_GIT_WORKSPACE_WARNING, request.warnings)

    def test_json_launch_does_not_emit_human_workspace_line(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        config = self._announcement_config()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.dict(os.environ, {"AI_PROFILE": ""}, clear=False),
            mock.patch.object(request_build, "load_config", return_value=(config, "test")),
            mock.patch.object(cli, "execute_request", return_value=(0, None)),
        ):
            code = cli.main(
                ["--json", "--cwd", repo.name, "droid", "work", "--model", "reviewer", "hello"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_continuity_mode_reaches_request_dry_run_and_run_context(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        parsed = cli_parser.parse_cli(
            [
                "--cwd",
                repo.name,
                "dry-run",
                "codex",
                "work",
                "--continuity-mode",
                "pinned",
                "x",
            ]
        )
        request = request_build.request_from_parsed(
            parsed,
            delegate_config.embedded_default_config(),
            io.StringIO(""),
        )

        self.assertEqual(request.continuity_mode, "pinned")
        self.assertEqual(cli.dry_run_payload(request)["continuityMode"], "pinned")

        default_request = request_build.request_from_parsed(
            cli_parser.parse_cli(["--cwd", repo.name, "dry-run", "codex", "work", "x"]),
            delegate_config.embedded_default_config(),
            io.StringIO(""),
        )
        self.assertEqual(default_request.continuity_mode, "fungible")
        self.assertEqual(cli.dry_run_payload(default_request)["continuityMode"], "fungible")

        registry_root = run_registry.ensure_registry(Path(repo.name), workspace_kind="git")
        with mock.patch.object(runner, "RunContext") as constructor:
            cli.make_run_context(
                registry_root,
                request,
                run_id="del_20260901T000000Z_abcdef",
                alias="codex-1",
                source_workspace=request_build.resolve_workspace(repo.name),
            )
        self.assertEqual(constructor.call_args.kwargs["continuity_mode"], "pinned")

    def assert_tracked_child_exited_and_safe_temp_dirs_cleaned(
        self,
        payload: dict,
        base: Path,
        *,
        producers: Sequence[object] = (),
    ) -> None:
        # The child was a real Delegate subprocess, so it resolved the
        # production compact child temp root; contain what its run manifest
        # recorded while this test's workspace registry still exists. The
        # `producers` are the reaped handles of the Delegate processes that
        # drove those runs: a run's compact temp is shared by all of its
        # attempts, so only those processes exiting makes deleting it safe.
        self.contain_compact_temps(payload["cwd"], producers=producers)
        pid = payload.get("pid")
        self.assertIsInstance(pid, int)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail(f"tracked child process {pid} is still running")

        while time.monotonic() < deadline:
            remaining = safe_temp_dirs(base)
            if not remaining:
                return
            time.sleep(0.02)
        self.assertEqual(safe_temp_dirs(base), set())

    def test_call_json_returns_text_without_registry(self):
        fake_bin = self.make_fake_bin()
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        parsed = cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"])
        request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
        call_workspace = Path(request.workspace)
        self.assertEqual(request.mode, "call")
        self.assertTrue(request.cleanup_workspace)
        self.assertTrue(call_workspace.is_dir())

        with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_ECHO_ARGS": "1"}):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "call")
        self.assertIn("OUT:", payload["text"])
        self.assertNotIn("alias", payload)
        self.assertNotIn("runId", payload)
        self.assertNotIn("snapshotCommand", payload)
        self.assertFalse(call_workspace.exists())

    def test_ungrouped_pinned_call_without_a_model_observation_warns(self):
        """An ungrouped call builds no run record, so the warning lands on its payload."""
        fake_bin = self.make_fake_bin()
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}

        def call(argv: list[str]) -> dict:
            parsed = cli_parser.parse_cli(argv)
            request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
            with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_ECHO_ARGS": "1"}):
                code, payload = cli.execute_request(
                    request,
                    json_mode=True,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=request_models.ResolvedWorkspace(
                        "<call-temp-cwd>", "directory"
                    ),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            return payload

        pinned = call(
            ["droid", "call", "--continuity-mode", "pinned", "--model", "reviewer", "hello"]
        )
        warnings = [w for w in pinned.get("warnings", []) if "pinned_continuity_unverified" in w]
        self.assertEqual(len(warnings), 1, pinned.get("warnings"))
        self.assertIn("droid reported no model event", warnings[0])
        self.assertIn("reviewer", warnings[0])

        fungible = call(["droid", "call", "--model", "reviewer", "hello"])
        self.assertFalse(
            [w for w in fungible.get("warnings", []) if "pinned_continuity_unverified" in w]
        )

    def test_call_with_repo_local_tmpdir_cleans_its_workspace(self):
        fake_bin = self.make_fake_bin()
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            temp_root = source / "tmp"
            temp_root.mkdir(parents=True)
            with (
                mock.patch.dict(os.environ, {"TMPDIR": str(temp_root)}, clear=False),
                mock.patch.object(tempfile, "tempdir", None),
            ):
                parsed = cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"])
                request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
            call_workspace = Path(request.workspace)
            self.assertTrue(call_workspace.is_relative_to(source))
            with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_ECHO_ARGS": "1"}):
                code, _payload = cli.execute_request(
                    request,
                    json_mode=True,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=request_models.ResolvedWorkspace(str(source), "directory"),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertFalse(call_workspace.exists())

    def test_call_missing_binary_cleans_temp_workspace(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        parsed = cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"])
        request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
        call_workspace = Path(request.workspace)
        self.assertTrue(call_workspace.is_dir())
        with (
            tempfile.TemporaryDirectory() as empty_path,
            mock.patch.dict(os.environ, {"PATH": empty_path}),
            self.assertRaises(errors.DelegateError) as ctx,
        ):
            cli.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(ctx.exception.error, "missing_binary")
        self.assertFalse(call_workspace.exists())

    def test_codex_call_json_reports_explicit_fast_choice(self):
        parsed = cli_parser.parse_cli(["codex", "call", "--no-fast", "hello"])
        request = request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO("")
        )
        fake_result = runner.CallResult(
            text="ok",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=2,
            stderr_bytes=0,
            text_chars=2,
            text_truncated=False,
            warnings=(),
        )
        with (
            mock.patch.object(cli, "ensure_binary"),
            mock.patch.object(runner, "execute_call", return_value=fake_result),
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertIs(payload["requestedFast"], False)
        self.assertNotIn("emptyRetry", payload)
        self.assertEqual(payload["resultQuality"], "ok")

    def test_call_json_reports_empty_retry_only_when_attempted(self):
        parsed = cli_parser.parse_cli(["codex", "call", "hello"])
        request = request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO("")
        )
        fake_result = runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=0,
            stderr_bytes=0,
            text_chars=0,
            text_truncated=False,
            result_quality="empty",
            empty_retry_attempted=True,
            empty_retry_resolved=False,
        )
        with (
            mock.patch.object(cli, "ensure_binary"),
            mock.patch.object(runner, "execute_call", return_value=fake_result),
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        # An unresolved empty is a failed call: the child exiting 0 with no
        # output must not publish ok/exit 0 (run_status.run_succeeded contract).
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exitCode"], 1)
        self.assertEqual(payload["error"], "empty_result")
        self.assertEqual(payload["resultQuality"], "empty")
        self.assertEqual(payload["emptyRetry"], {"attempted": True, "resolved": False})

    def test_call_json_no_assistant_text_fails_without_retry_metadata(self):
        parsed = cli_parser.parse_cli(["codex", "call", "hello"])
        request = request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO("")
        )
        fake_result = runner.CallResult(
            text="",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=120,
            stderr_bytes=0,
            text_chars=0,
            text_truncated=False,
            result_quality="no_assistant_text",
        )
        with (
            mock.patch.object(cli, "ensure_binary"),
            mock.patch.object(runner, "execute_call", return_value=fake_result),
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        # resultQuality must be visible even when no retry ran (skipped-retry
        # empties were previously invisible to JSON consumers).
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "empty_result")
        self.assertEqual(payload["resultQuality"], "no_assistant_text")
        self.assertNotIn("emptyRetry", payload)

    def _call_temp_dirs(self):
        return set(Path(tempfile.gettempdir()).glob("delegate-call-*"))

    def test_call_build_failure_cleans_temp_workspace(self):
        # A request-build failure AFTER _call_workspace() (e.g. unknown alias) must
        # not orphan the freshly created temp cwd.
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        parsed = cli_parser.parse_cli(
            ["droid", "call", "--model", "replace-with-model-id", "hello"]
        )
        before = self._call_temp_dirs()
        with self.assertRaises(errors.DelegateError) as ctx:
            request_build.request_from_parsed(parsed, config, io.StringIO(""))
        self.assertEqual(ctx.exception.error, "unconfigured_model")
        self.assertEqual(self._call_temp_dirs() - before, set())

    def test_codex_call_default_is_work_level_sandbox(self):
        argv = argv_builders.build_codex_argv(
            delegate_config.embedded_default_config()["codex"],
            "call",
            "/tmp/call",
            None,
            "do this",
            {},
            workspace_kind="directory",
        )
        self.assertEqual(
            argv[argv.index("--sandbox") + 1],
            delegate_config.embedded_default_config()["codex"]["workSandbox"],
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_call_read_only_is_read_only_sandbox(self):
        argv = argv_builders.build_codex_argv(
            delegate_config.embedded_default_config()["codex"],
            "call",
            "/tmp/call",
            None,
            "score",
            {},
            workspace_kind="directory",
            call_read_only=True,
        )
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_grok_call_default_vs_read_only(self):
        default_argv = argv_builders.build_grok_argv(
            delegate_config.embedded_default_config()["grok"], "call", "/tmp/call", None, {}
        )
        self.assertIn("auto", default_argv)
        self.assertNotIn("read-only", default_argv)
        ro_argv = argv_builders.build_grok_argv(
            delegate_config.embedded_default_config()["grok"],
            "call",
            "/tmp/call",
            None,
            {},
            call_read_only=True,
        )
        self.assertIn("read-only", ro_argv)
        self.assertIn("dontAsk", ro_argv)

    def test_claude_call_default_vs_read_only(self):
        default_argv = argv_builders.build_claude_argv(
            delegate_config.embedded_default_config()["claude"], "call", None, {}
        )
        self.assertIn("auto", default_argv)
        self.assertNotIn("plan", default_argv)
        ro_argv = argv_builders.build_claude_argv(
            delegate_config.embedded_default_config()["claude"],
            "call",
            None,
            {},
            call_read_only=True,
        )
        self.assertIn("plan", ro_argv)
        self.assertIn("--strict-mcp-config", ro_argv)

    def test_cursor_and_droid_call_write_flags_only_when_not_read_only(self):
        cursor_default = argv_builders.build_cursor_argv(["cursor-agent"], "call", "/ws", "model")
        self.assertIn("--force", cursor_default)
        cursor_ro = argv_builders.build_cursor_argv(
            ["cursor-agent"], "call", "/ws", "model", call_read_only=True
        )
        self.assertNotIn("--force", cursor_ro)
        droid_default = argv_builders.build_droid_argv("droid", "call", "/ws", "m", "p")
        self.assertIn("--skip-permissions-unsafe", droid_default)
        droid_ro = argv_builders.build_droid_argv(
            "droid", "call", "/ws", "m", "p", call_read_only=True
        )
        self.assertNotIn("--skip-permissions-unsafe", droid_ro)

    def test_read_only_call_prepends_neutralizing_preamble_default_call_is_raw(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        ro = request_build.request_from_parsed(
            cli_parser.parse_cli(["codex", "call", "--read-only", "Score this diff."]),
            config,
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, ro.workspace, ignore_errors=True)
        self.assertTrue(ro.stdin_text.startswith("You are being called"))
        raw = request_build.request_from_parsed(
            cli_parser.parse_cli(["codex", "call", "Score this diff."]),
            config,
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, raw.workspace, ignore_errors=True)
        self.assertEqual(raw.stdin_text, "Score this diff.")

    def test_default_call_inherits_work_policy_read_only_call_inherits_safe(self):
        # Default call is work-level, so it must inherit work-tier policy
        # (webSearch); read-only call is safe-level and must not.
        config = delegate_config.deep_merge(
            delegate_config.embedded_default_config(),
            {"policy": {"work": {"webSearch": True}}},
        )
        default_req = request_build.request_from_parsed(
            cli_parser.parse_cli(["codex", "call", "do this"]), config, io.StringIO("")
        )
        self.addCleanup(shutil.rmtree, default_req.workspace, ignore_errors=True)
        self.assertIn('web_search="live"', default_req.argv)
        # ...but never a bypass, even at work-tier policy.
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", default_req.argv)
        ro_req = request_build.request_from_parsed(
            cli_parser.parse_cli(["codex", "call", "--read-only", "score"]),
            config,
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, ro_req.workspace, ignore_errors=True)
        self.assertNotIn('web_search="live"', ro_req.argv)

    def test_read_only_flag_rejected_outside_call_mode(self):
        for mode in ("safe", "work"):
            with self.subTest(mode=mode):
                parsed = cli_parser.parse_cli(["codex", mode, "--read-only", "x"])
                with self.assertRaises(errors.DelegateError) as ctx:
                    request_build.request_from_parsed(
                        parsed, delegate_config.embedded_default_config(), io.StringIO("")
                    )
                self.assertEqual(ctx.exception.error, "invalid_option_combination")

    def test_call_json_surfaces_truncation_fields(self):
        fake_bin = self.make_fake_bin()
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        parsed = cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"])
        request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
        with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_ECHO_ARGS": "1"}):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertIn("textChars", payload)
        self.assertIn("textTruncated", payload)
        self.assertIsInstance(payload["textChars"], int)
        self.assertFalse(payload["textTruncated"])

    def test_pi_family_call_json_populates_assistant_text(self):
        fake_result = runner.CallResult(
            text="FAMILY_OK",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=5,
            stderr_bytes=0,
            text_chars=5,
            text_truncated=False,
        )
        for engine in ("pi", "omp"):
            with self.subTest(engine=engine):
                request = request_build.build_request(
                    engine,
                    "call",
                    None,
                    request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                    "hello",
                    delegate_config.embedded_default_config(),
                    False,
                )
                with (
                    mock.patch.object(cli, "ensure_binary"),
                    mock.patch.object(
                        runner,
                        "execute_call",
                        return_value=fake_result,
                    ),
                ):
                    code, payload = cli.execute_request(
                        request,
                        json_mode=True,
                        config=delegate_config.embedded_default_config(),
                        pass_through=False,
                        completion_report_mode="none",
                        source_workspace=request_models.ResolvedWorkspace(
                            "<call-temp-cwd>", "directory"
                        ),
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

                self.assertEqual(code, 0)
                self.assertEqual(payload["text"], "FAMILY_OK")
                self.assertEqual(payload["assistantText"], "FAMILY_OK")

    def test_call_failure_surfaces_stderr_tail_in_json_and_text(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        for name in ("droid", "agent"):
            path = bin_dir / name
            path.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'Authorization: Bearer abcdefghijklmnop\\n' >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
            path.chmod(0o755)
        env_path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}

        json_request = request_build.request_from_parsed(
            cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"]),
            config,
            io.StringIO(""),
        )
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, payload = cli.execute_request(
                json_request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 7)
        self.assertIn("Authorization: ***", payload["stderrTail"])
        self.assertNotIn("abcdefghijklmnop", payload["stderrTail"])

        text_request = request_build.request_from_parsed(
            cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"]),
            config,
            io.StringIO(""),
        )
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": env_path}):
            code, _payload = cli.execute_request(
                text_request,
                json_mode=False,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=stderr,
            )
        self.assertEqual(code, 7)
        self.assertIn("Authorization: ***", stderr.getvalue())
        self.assertNotIn("abcdefghijklmnop", stderr.getvalue())
        self.assertEqual(len([line for line in stderr.getvalue().splitlines() if line]), 1)

    def test_json_success_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        workspace = request_build.resolve_workspace(repo.name)
        request = request_models.Request(
            "droid",
            "safe",
            repo.name,
            "SECRET PROMPT VALUE",
            [
                "droid",
                "exec",
                "--cwd",
                repo.name,
                "--model",
                "model-id",
                "SECRET PROMPT VALUE",
            ],
            "model-id",
        )
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"PATH": env_path, "FAKE_ECHO_ARGS": "1", "FAKE_ASSISTANT_RESULT": "1"},
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=stdout_buf,
                stderr=stderr_buf,
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertIn("alias", payload)
        self.assertIn("runId", payload)
        self.assertIn("snapshotCommand", payload)
        self.assertEqual(payload["exitCode"], 0)
        self.assertGreater(payload["stdoutBytes"], 0)
        self.assertGreater(payload["stderrBytes"], 0)
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        payload_text = json.dumps(payload)
        self.assertNotIn("SECRET PROMPT VALUE", payload_text)
        self.assertNotIn("SECRET PROMPT VALUE", stdout_buf.getvalue())
        self.assertNotIn("SECRET PROMPT VALUE", stderr_buf.getvalue())

    def test_json_failure_shape_with_fake_binary(self):
        repo = make_git_repo()
        fake_bin = self.make_fake_bin()
        self.addCleanup(repo.cleanup)
        env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        workspace = request_build.resolve_workspace(repo.name)
        request = request_models.Request(
            "droid",
            "safe",
            repo.name,
            "hello",
            ["droid", "exec", "--cwd", repo.name, "--model", "model-id", "hello"],
            "model-id",
        )
        with mock.patch.dict(os.environ, {"PATH": env_path, "FAKE_EXIT": "7"}):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=delegate_config.embedded_default_config(),
                pass_through=False,
                completion_report_mode="markdown",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 7)
        self.assertIsNotNone(payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "child_failed")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exitCode"], 7)

    def test_run_input_json_rejects_unknown_keys(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        task = Path(repo.name) / "task.json"
        task.write_text(
            json.dumps(
                {
                    "engine": "droid",
                    "mode": "safe",
                    "model": "minimax",
                    "cwd": repo.name,
                    "prompt": "hello",
                    "promtp": "typo",
                }
            )
        )
        parsed = request_models.ParsedCommand(
            "run",
            global_options=request_models.GlobalOptions(json_mode=True),
            payload=request_models.RunJsonOptions(str(task)),
        )
        with self.assertRaises(errors.DelegateError) as ctx:
            request_build.request_from_input_json(parsed, delegate_config.embedded_default_config())
        self.assertEqual(ctx.exception.error, "unknown_input_key")

    def test_static_safety_guards(self):
        source = MODULE_PATH.read_text()
        forbidden = [
            "subprocess.Popen",
            "start_new_session",
            "shell=True",
            "git push",
            "git commit",
            "git merge",
        ]
        for text in forbidden:
            with self.subTest(text=text):
                self.assertNotIn(text, source)

    def test_cursor_safe_json_reports_source_workspace_not_temp_copy(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_cursor_safe_fake_agent()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        base = self.private_tmp_env(env)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "cursor",
                "safe",
                "review",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["workspaceKind"], "git")
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assert_tracked_child_exited_and_safe_temp_dirs_cleaned(
            payload, base, producers=[completed]
        )

    def test_cursor_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        tracked = Path(repo.name) / "tracked.txt"
        tracked.write_text("before\n")
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "add", "tracked.txt"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked.write_text("dirty\n")
        untracked = Path(repo.name) / "notes.txt"
        untracked.write_text("local-only\n")

        fake_bin = self.make_cursor_safe_fake_agent()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        base = self.private_tmp_env(env)

        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--cwd", repo.name, "cursor", "safe", "review"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.contain_compact_temps(repo.name, producers=[completed])
        self.assertEqual(completed.returncode, 0)
        self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
        self.assertEqual(tracked.read_text(), "dirty\n")
        self.assertEqual(untracked.read_text(), "local-only\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not safe_temp_dirs(base):
                break
            time.sleep(0.02)
        self.assertEqual(safe_temp_dirs(base), set())

    def test_cursor_safe_directory_execution_does_not_mutate_original_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "source.txt"
            source.write_text("keep-me\n")
            fake_bin = self.make_cursor_safe_fake_agent()
            config = Path(workspace) / "config.json"
            config.write_text(json.dumps(delegate_config.embedded_default_config()))
            env = os.environ.copy()
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["DELEGATE_CONFIG"] = str(config)
            base = self.private_tmp_env(env)

            completed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--cwd", workspace, "cursor", "safe", "review"],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.contain_compact_temps(workspace, producers=[completed])
            self.assertEqual(completed.returncode, 0)
            self.assertFalse((Path(workspace) / "mutated-by-agent.txt").exists())
            self.assertEqual(source.read_text(), "keep-me\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not safe_temp_dirs(base):
                    break
                time.sleep(0.02)
            self.assertEqual(safe_temp_dirs(base), set())

    def test_cursor_work_explicit_cwd_under_git_parent_writes_in_literal_directory(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        launch_cwd = Path(repo.name) / "research-lane"
        launch_cwd.mkdir()
        fake_dir = tempfile.TemporaryDirectory()
        self.addCleanup(fake_dir.cleanup)
        fake_bin = Path(fake_dir.name)
        # Three ways a real engine finds its root: spawn cwd, WORKSPACE_ROOT,
        # and its own --workspace argv. Each must land in the launch dir.
        (fake_bin / "agent").write_text(
            "#!/usr/bin/env bash\n"
            "touch mutated-by-agent.txt\n"
            '(cd "$WORKSPACE_ROOT" && touch mutated-via-env.txt)\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--workspace" ]; then (cd "$2" && touch mutated-via-argv.txt); fi\n'
            "  shift\n"
            "done\n"
            # A cursor `result` event with genuine assistant text, so the run's
            # resultQuality is `ok` rather than the empty-output failure the
            # outcome contract now gives an exit-0 child with no assistant text.
            'printf \'{"type":"result","result":"Status: completed\\\\n'
            "- literal-directory fake\"}\\n'\n"
        )
        (fake_bin / "agent").chmod(0o755)
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)

        dry = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                str(launch_cwd),
                "--json",
                "dry-run",
                "cursor",
                "work",
                "write the research output",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        dry_payload = json.loads(dry.stdout)
        self.assertEqual(Path(dry_payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertEqual(Path(dry_payload["executionCwd"]).resolve(), launch_cwd.resolve())

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                str(launch_cwd),
                "--json",
                "cursor",
                "work",
                "write the research output",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.contain_compact_temps(repo.name, producers=[completed])

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertEqual(Path(payload["executionCwd"]).resolve(), launch_cwd.resolve())
        for name in ("mutated-by-agent.txt", "mutated-via-env.txt", "mutated-via-argv.txt"):
            self.assertTrue((launch_cwd / name).is_file(), name)
            self.assertFalse((Path(repo.name) / name).exists(), name)

    def make_codex_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "codex"
        path.write_text(
            "#!/usr/bin/env bash\n"
            'dir="$PWD"\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    --cd) dir="$2"; shift 2 ;;\n'
            '    -C) dir="$2"; shift 2 ;;\n'
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            'touch "$dir/mutated-by-codex.txt"\n'
            'printf \'{"type":"item.completed","item":{"type":"agent_message","text":"Status: completed\\\\n- codex fake"}}\\n\'\n'
            'printf \'{"type":"turn.completed"}\\n\'\n'
            "exit 0\n"
        )
        path.chmod(0o755)
        return bin_dir

    def make_claude_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "claude"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "touch mutated-by-claude.txt\n"
            'prompt="$(cat)"\n'
            'printf \'%s\\n\' \'{"type":"system","cwd":"fake"}\'\n'
            'printf \'{"type":"assistant","message":{"content":[{"type":"text","text":"saw stdin: %s"}]}}\\n\' "$prompt"\n'
            'printf \'%s\\n\' \'{"type":"result","subtype":"success","result":"Status: completed\\\\n- final from claude"}\'\n'
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return bin_dir

    def test_codex_safe_default_argv_uses_read_only_sandbox_without_network_or_bypasses(self):
        policy = delegate_config.effective_policy(
            delegate_config.embedded_default_config(),
            engine="codex",
            mode="safe",
        )
        argv = argv_builders.build_codex_argv(
            delegate_config.embedded_default_config()["codex"],
            "safe",
            "/repo",
            None,
            "review only",
            policy,
            workspace_kind="git",
        )
        self.assertIn('approval_policy="never"', argv[argv.index("exec") :])
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)
        self.assertNotIn("sandbox_workspace_write.network_access=true", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)

    def test_codex_safe_argv_never_emits_bypass_flags_even_if_policy_sets_them(self):
        # Config validation rejects bypass flags under safe mode, but the argv
        # builder must also refuse to emit them structurally — safe mode stays
        # read-only no matter what a policy dict carries.
        argv = argv_builders.build_codex_argv(
            delegate_config.embedded_default_config()["codex"],
            "safe",
            "/repo",
            None,
            "review only",
            {
                "bypassApprovalsAndSandbox": True,
                "bypassHookTrust": True,
            },
            workspace_kind="git",
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("read-only", argv)

    def test_claude_safe_default_argv_uses_plan_permissions_and_stdin(self):
        argv = argv_builders.build_claude_argv(
            delegate_config.embedded_default_config()["claude"],
            "safe",
            "claude-opus-4-8",
            {"bypassApprovalsAndSandbox": True},
            stream_capture=True,
            reasoning_effort="xhigh",
        )
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--input-format", argv)
        self.assertIn("text", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("plan", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--tools", argv)
        tools = argv[argv.index("--tools") + 1]
        self.assertIn("Read", tools)
        self.assertIn("Grep", tools)
        self.assertIn("Glob", tools)
        self.assertIn("Bash", tools)
        self.assertIn("--allowedTools", argv)
        allowed_tools = argv[argv.index("--allowedTools") + 1]
        self.assertIn("Bash(git diff:*)", allowed_tools)
        self.assertIn("Bash(git status:*)", allowed_tools)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--model", argv)
        self.assertIn("claude-opus-4-8", argv)
        self.assertIn("--effort", argv)
        self.assertIn("xhigh", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_claude_work_does_not_bypass_from_global_policy(self):
        argv = argv_builders.build_claude_argv(
            delegate_config.embedded_default_config()["claude"],
            "work",
            None,
            {"bypassApprovalsAndSandbox": True},
        )
        self.assertIn("--permission-mode", argv)
        self.assertIn("auto", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_claude_work_uses_harness_scoped_policy_bypass(self):
        argv = argv_builders.build_claude_argv(
            delegate_config.embedded_default_config()["claude"],
            "work",
            None,
            {"bypassApprovalsAndSandbox": True},
            allow_bypass_permissions=True,
        )
        self.assertIn("--permission-mode", argv)
        self.assertIn("bypassPermissions", argv)

    def test_claude_work_external_sandbox_profile_does_not_bypass(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["policy"]["profile"] = "external-sandbox"
        request = self.build_git_request(
            "claude",
            "work",
            None,
            "/repo",
            "ship",
            config,
            dry_run=True,
        )
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("auto", request.argv)
        self.assertNotIn("bypassPermissions", request.argv)

    def test_claude_describe_runtime_bypass_no_drift(self):
        def assert_bypass(config, expected):
            runtime = describe_payload._claude_runtime_policy(config, "work")
            harness = argv_builders._claude_harness_bypass_enabled(config, "work")
            self.assertEqual(runtime["bypassApprovalsAndSandbox"], expected)
            self.assertEqual(harness, expected)
            self.assertEqual(runtime["bypassApprovalsAndSandbox"], harness)

        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        assert_bypass(config, False)

        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config.setdefault("policy", {})
        config["policy"].setdefault("harness", {})
        config["policy"]["harness"].setdefault("claude", {})
        config["policy"]["harness"]["claude"]["work"] = {
            "bypassApprovalsAndSandbox": True,
        }
        assert_bypass(config, True)

        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config.setdefault("policy", {})
        config["policy"]["profile"] = "external-sandbox"
        config["policy"].setdefault("work", {})
        config["policy"]["work"]["bypassApprovalsAndSandbox"] = True
        assert_bypass(config, False)

    def test_claude_work_harness_policy_allows_bypass(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["policy"]["harness"] = {"claude": {"work": {"bypassApprovalsAndSandbox": True}}}
        request = self.build_git_request(
            "claude",
            "work",
            None,
            "/repo",
            "ship",
            config,
            dry_run=True,
        )
        self.assertIn("--permission-mode", request.argv)
        self.assertIn("bypassPermissions", request.argv)

    def test_claude_config_rejects_bypass_permission_mode(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["claude"]["workPermissionMode"] = "bypassPermissions"
        with self.assertRaises(errors.DelegateError) as ctx:
            request_build.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_claude_config")
        self.assertIn(
            "policy.harness.claude.work.bypassApprovalsAndSandbox",
            ctx.exception.message,
        )

    def test_claude_request_uses_stdin_transport_without_prompt_in_argv(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["claude"]["defaultModel"] = "claude-sonnet-4-6"
        request = self.build_git_request(
            "claude",
            "safe",
            None,
            "/repo",
            "SECRET CLAUDE PROMPT",
            config,
            dry_run=True,
            reasoning_effort="high",
            reasoning_effort_source="cli",
        )
        self.assertEqual(request.prompt_transport, prompt_transport.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, "SECRET CLAUDE PROMPT")
        self.assertNotIn("SECRET CLAUDE PROMPT", request.argv)
        self.assertEqual(request.reasoning_effort, "high")
        self.assertEqual(request.reasoning_transport, "claude-effort-flag")
        self.assertEqual(request.reasoning_capability_source, "harness-compatibility")
        self.assertEqual(request.reasoning_capability_evidence, "harness")
        self.assertIn("--effort", request.argv)
        self.assertIn("high", request.argv)

    def test_codex_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_codex_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        base = self.private_tmp_env(env)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "codex",
                "safe",
                "review",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.contain_compact_temps(repo.name, producers=[completed])
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse((Path(repo.name) / "mutated-by-codex.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        # argv structure assertions live in the dry-run and unit tests; the tracked
        # run JSON summary does not surface argv at the top level (matches Cursor's
        # safe-mutation test).
        self.assertEqual(safe_temp_dirs(base), set())

    def test_claude_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_claude_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        base = self.private_tmp_env(env)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "claude",
                "safe",
                "review",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse((Path(repo.name) / "mutated-by-claude.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assert_tracked_child_exited_and_safe_temp_dirs_cleaned(
            payload, base, producers=[completed]
        )

    def make_kimi_safe_fake(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / "kimi"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "touch mutated-by-kimi.txt\n"
            'printf \'{"role":"assistant","content":"Status: completed\\\\n- kimi fake"}\\n\'\n'
            "printf 'OUT:%s\\n' \"$*\"\n"
            'exit "${FAKE_EXIT:-0}"\n'
        )
        path.chmod(0o755)
        return bin_dir

    def test_kimi_safe_git_execution_does_not_mutate_original_workspace(self):
        repo = make_git_repo()
        self.addCleanup(repo.cleanup)
        subprocess.run(
            ["git", "-C", repo.name, *GIT_TEST_IDENTITY, "commit", "--allow-empty", "-m", "init"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_bin = self.make_kimi_safe_fake()
        config = Path(repo.name) / "config.json"
        config.write_text(json.dumps(delegate_config.embedded_default_config()))
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["DELEGATE_CONFIG"] = str(config)
        base = self.private_tmp_env(env)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--cwd",
                repo.name,
                "--json",
                "kimi",
                "safe",
                "review",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse((Path(repo.name) / "mutated-by-kimi.txt").exists())
        self.assertTrue(payload.get("isolatedWorkspace"))
        self.assertEqual(Path(payload["cwd"]).resolve(), Path(repo.name).resolve())
        self.assertIn("executionCwd", payload)
        self.assertNotEqual(payload["executionCwd"], payload["cwd"])
        self.assert_tracked_child_exited_and_safe_temp_dirs_cleaned(
            payload, base, producers=[completed]
        )

    def test_effective_prompt_codex_safe_order(self):
        user = "review the diff"
        p = request_build.effective_prompt(
            user, engine="codex", mode="safe", completion_report_mode="markdown"
        )
        self.assertIn("Delegate sub-agent skill review", p)
        self.assertIn("safe/read-only mode", p)
        self.assertIn("must not override the read-only requirement", p)
        codex_idx = p.find("Delegate Codex safe mode")
        user_idx = p.find("review the diff")
        suffix_idx = p.find("Delegate completion report requirement")
        self.assertGreater(codex_idx, 0)
        self.assertGreater(user_idx, codex_idx)
        self.assertGreater(suffix_idx, user_idx)

    def test_effective_prompt_codex_safe_idempotent(self):
        # effective_prompt run twice on the same string must not double-inject the
        # codex safe prefix. prepend_skill_review_instructions is already idempotent;
        # the codex inject must be too.
        once = request_build.effective_prompt(
            "review the diff",
            engine="codex",
            mode="safe",
            completion_report_mode="none",
        )
        twice = request_build.effective_prompt(
            once,
            engine="codex",
            mode="safe",
            completion_report_mode="none",
        )
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Codex safe mode"), 1)

    def test_effective_prompt_claude_safe_order_and_idempotence(self):
        once = request_build.effective_prompt(
            "review the diff",
            engine="claude",
            mode="safe",
            completion_report_mode="none",
        )
        twice = request_build.effective_prompt(
            once,
            engine="claude",
            mode="safe",
            completion_report_mode="none",
        )
        claude_idx = once.find("Delegate Claude safe mode")
        user_idx = once.find("review the diff")
        self.assertGreater(claude_idx, 0)
        self.assertGreater(user_idx, claude_idx)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Claude safe mode"), 1)

    def test_effective_prompt_droid_safe_order_and_idempotence(self):
        once = request_build.effective_prompt(
            "review the diff",
            engine="droid",
            mode="safe",
            completion_report_mode="none",
        )
        twice = request_build.effective_prompt(
            once,
            engine="droid",
            mode="safe",
            completion_report_mode="none",
        )
        droid_idx = once.find("Delegate Droid safe mode")
        user_idx = once.find("review the diff")
        self.assertGreater(droid_idx, 0)
        self.assertGreater(user_idx, droid_idx)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("Delegate Droid safe mode"), 1)

    def test_effective_prompt_codex_work_omits_safe_prefix(self):
        p = request_build.effective_prompt(
            "ship the fix",
            engine="codex",
            mode="work",
            completion_report_mode="none",
        )
        self.assertNotIn("Delegate Codex safe mode", p)

    def test_effective_prompt_cursor_safe_omits_codex_prefix(self):
        p = request_build.effective_prompt(
            "review the diff",
            engine="cursor",
            mode="safe",
            completion_report_mode="none",
        )
        self.assertNotIn("Delegate Codex safe mode", p)

    def test_codex_missing_binary_exit_3(self):
        request = request_models.Request(
            "codex",
            "work",
            "/repo",
            "hello",
            ["delegate-definitely-missing-codex", "exec", "hello"],
            None,
        )
        with self.assertRaises(errors.DelegateError) as ctx:
            cli.ensure_binary(request.argv)
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_missing_binary_error_includes_config_fix_diagnostics(self):
        config_path = "/tmp/delegate-config.json"
        with self.assertRaises(errors.DelegateError) as ctx:
            cli.ensure_binary(
                ["delegate-definitely-missing-claude", "-p"],
                engine="claude",
                config_source=config_path,
            )
        error = ctx.exception
        self.assertEqual(error.error, "missing_binary")
        self.assertEqual(error.exit_code, errors.EXIT_MISSING_BINARY)
        self.assertIn("searched PATH of the delegate process", error.message)
        self.assertIn("claude.binary", error.message)
        self.assertEqual(error.diagnostics["configPath"], config_path)
        self.assertEqual(error.diagnostics["configKey"], "claude.binary")

    def test_ensure_binary_uses_profiled_child_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "profile-bin"
            empty_path = root / "empty-path"
            bin_dir.mkdir()
            empty_path.mkdir()
            binary = bin_dir / "profile-agent"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with mock.patch.dict(os.environ, {"PATH": str(empty_path)}):
                cli.ensure_binary(
                    ["profile-agent"],
                    env_overrides={"PATH": str(bin_dir)},
                )

            with (
                mock.patch.dict(os.environ, {"PATH": str(bin_dir)}),
                self.assertRaises(errors.DelegateError) as ctx,
            ):
                cli.ensure_binary(
                    ["profile-agent"],
                    env_overrides={"PATH": str(empty_path)},
                )
            self.assertEqual(ctx.exception.error, "missing_binary")
            self.assertIn("searched PATH of the child environment", ctx.exception.message)

    def test_missing_binary_json_includes_candidate_path(self):
        with tempfile.TemporaryDirectory() as home:
            config_path = Path(home) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "kimi": {"binary": "delegate-test-kimi"},
                        "isolation": {"safe": "auto"},
                    }
                ),
                encoding="utf-8",
            )
            candidate_dir = Path(home) / ".kimi-code" / "bin"
            candidate_dir.mkdir(parents=True)
            candidate = candidate_dir / "delegate-test-kimi"
            candidate.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            candidate.chmod(0o755)
            empty_path = Path(home) / "empty-path"
            empty_path.mkdir()
            stdout_buf = io.StringIO()

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": home,
                    "DELEGATE_CONFIG": str(config_path),
                    "PATH": str(empty_path),
                },
            ):
                code = cli.main(
                    ["--json", "--cwd", home, "kimi", "safe", "hello"],
                    stdout=stdout_buf,
                )

        payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(code, errors.EXIT_MISSING_BINARY)
        self.assertEqual(payload["error"], "missing_binary")
        self.assertEqual(payload["configPath"], str(config_path))
        self.assertEqual(payload["configKey"], "kimi.binary")
        self.assertEqual(payload["suggestedBinaryPath"], str(candidate))
        self.assertIn(str(candidate), payload["message"])

    def test_call_mode_warning_merge_dedupes_preserving_order(self):
        # F7: call-mode warning merge dedupes while preserving order. A warning
        # present in both request.warnings and result.warnings is emitted once.
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["droid"]["models"] = {"reviewer": "model-id"}
        parsed = cli_parser.parse_cli(["droid", "call", "--model", "reviewer", "hello"])
        request = request_build.request_from_parsed(parsed, config, io.StringIO(""))
        duplicate_warning = "shared warning from both channels"
        request = dataclasses.replace(
            request,
            warnings=(duplicate_warning, "request-only warning"),
            cleanup_workspace=False,
        )
        fake_result = runner.CallResult(
            text="ok",
            exit_code=0,
            duration_ms=10,
            stdout_bytes=2,
            stderr_bytes=0,
            text_chars=2,
            text_truncated=False,
            warnings=(duplicate_warning, "result-only warning"),
        )
        with (
            mock.patch.object(cli, "ensure_binary"),
            mock.patch.object(runner, "execute_call", return_value=fake_result),
        ):
            code, payload = cli.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=request_models.ResolvedWorkspace("<call-temp-cwd>", "directory"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["warnings"],
            ["shared warning from both channels", "request-only warning", "result-only warning"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
