import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


WORKER = Path(__file__).resolve().parents[1] / "remote_worker.py"
SPEC = importlib.util.spec_from_file_location("remote_worker_sync", WORKER)
remote_worker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = remote_worker
SPEC.loader.exec_module(remote_worker)


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SyncRemoteTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sdk"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(self.root), "config", key, value], check=True)
        (self.root / "tracked.txt").write_text("base\n")
        build = self.root / "build"
        build.mkdir()
        (build / "envsetup.sh").write_text(
            "lunch() { export OUT=\"$PWD/out\"; }\n"
            "m() { mkdir -p \"$OUT/system/lib64\"; printf built > \"$OUT/system/lib64/libfoo.so\"; }\n"
        )
        product = self.root / "out/system"
        product.mkdir(parents=True)
        (product / "build.prop").write_text("ro.build.version.sdk=30\nro.build.fingerprint=fake/fp\n")
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        self.base = command("git", "-C", str(self.root), "rev-parse", "HEAD")
        self.job_dir = Path(self.temporary.name) / "job"
        self.job_dir.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _target_bundle(self, files, force_add=()):
        local = Path(self.temporary.name) / "local"
        subprocess.run(["git", "clone", "-q", str(self.root), str(local)], check=True)
        subprocess.run(["git", "-C", str(local), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(local), "config", "user.name", "Test"], check=True)
        for name, value in files.items():
            path = local / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
            subprocess.run(["git", "-C", str(local), "add", "-f" if name in force_add else "--", name], check=True)
        subprocess.run(["git", "-C", str(local), "commit", "-qm", "target"], check=True)
        target = command("git", "-C", str(local), "rev-parse", "HEAD")
        ref = "refs/kbe-deploy/test-target"
        subprocess.run(["git", "-C", str(local), "update-ref", ref, target], check=True)
        bundle = self.job_dir / "source.bundle"
        subprocess.run(["git", "-C", str(local), "bundle", "create", str(bundle), ref, "^" + self.base], check=True)
        subprocess.run(["git", "-C", str(local), "update-ref", "-d", ref, target], check=True)
        return target

    def _write_job(self, *, target=None, files=None, mode="local", expected=None):
        before = remote_worker.inspect_source(self.root) if expected is None else expected
        sync = None if target is None else {
            "strategy": "ff-only", "base_head": self.base, "target_head": target,
            "bundle_sha256": sha256(self.job_dir / "source.bundle") if target != self.base else None,
        }
        source = {"mode": mode, "expected_head": before["head"], "expected_dirty_digest": before["dirty_digest"], "files": files or []}
        if sync is not None:
            source["sync"] = sync
        job = {
            "id": "sync-job", "action": "build",
            "platform": {"id": "fake", "remote_root": str(self.root), "lunch": "fake-userdebug", "product_out": "out"},
            "modules": [{"id": "foo", "targets": ["fake_target"], "artifacts": [{"path": "system/lib64/libfoo.so", "kind": "elf", "bits": 64}]}],
            "jobs": 1, "source": source,
        }
        (self.job_dir / "job.json").write_text(json.dumps(job))

    def _run(self):
        with mock.patch.object(remote_worker.sys, "platform", "linux"):
            return remote_worker.run_job(self.job_dir)

    def test_fast_forward_unpushed_bundle_and_old_job_compatibility(self):
        target = self._target_bundle({"tracked.txt": "target\n"})
        self._write_job(target=target)
        manifest = self._run()
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), target)
        self.assertEqual(manifest["source"]["observed_before"]["head"], self.base)
        self.assertEqual(manifest["source"]["observed_build_base"]["head"], target)
        sync = json.loads((self.job_dir / "sync.json").read_text())
        self.assertEqual(sync["state"], "synced")
        # A pre-sync job remains accepted and leaves the current checkout alone.
        second = Path(self.temporary.name) / "old-job"
        second.mkdir()
        self.job_dir = second
        self._write_job(mode="remote")
        self.assertEqual(self._run()["source"]["observed_before"]["head"], target)

    def test_overlay_restores_the_synced_baseline(self):
        target = self._target_bundle({"tracked.txt": "target\n"})
        overlay = self.job_dir / "overlay"
        overlay.mkdir()
        overlay_bytes = b"overlay\n"
        (overlay / "tracked.txt").write_bytes(overlay_bytes)
        self._write_job(target=target, mode="overlay", files=[{
            "path": "tracked.txt", "sha256": hashlib.sha256(overlay_bytes).hexdigest(),
            "baseline_sha256": hashlib.sha256(b"target\n").hexdigest(),
        }])
        self._run()
        self.assertEqual((self.root / "tracked.txt").read_text(), "target\n")
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), target)

    def test_dirty_and_diverged_sources_are_refused_without_moving_head(self):
        target = self._target_bundle({"tracked.txt": "target\n"})
        (self.root / "tracked.txt").write_text("dirty\n")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "clean tracked"):
            self._run()
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), self.base)
        receipt = json.loads((self.job_dir / "sync.json").read_text())
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["actual_head"], self.base)

        self.temporary.cleanup()
        self.setUp()
        old_base = self.base
        (self.root / "ahead.txt").write_text("ahead\n")
        subprocess.run(["git", "-C", str(self.root), "add", "ahead.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ahead"], check=True)
        self.base = command("git", "-C", str(self.root), "rev-parse", "HEAD")
        local = Path(self.temporary.name) / "diverged"
        subprocess.run(["git", "clone", "-q", str(self.root), str(local)], check=True)
        subprocess.run(["git", "-C", str(local), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(local), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(local), "checkout", "-qb", "other", old_base], check=True)
        (local / "other.txt").write_text("other\n")
        subprocess.run(["git", "-C", str(local), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(local), "commit", "-qm", "other"], check=True)
        target = command("git", "-C", str(local), "rev-parse", "HEAD")
        ref = "refs/kbe-deploy/diverged"
        subprocess.run(["git", "-C", str(local), "update-ref", ref, target], check=True)
        subprocess.run(["git", "-C", str(local), "bundle", "create", str(self.job_dir / "source.bundle"), ref, "^" + self.base], check=True)
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "ancestor"):
            self._run()
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), self.base)

    def test_untracked_and_ignored_conflicts_are_refused(self):
        target = self._target_bundle({"collision.txt": "tracked\n"})
        (self.root / "collision.txt").write_text("untracked\n")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "untracked or ignored"):
            self._run()
        self.assertEqual((self.root / "collision.txt").read_text(), "untracked\n")
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), self.base)

        self.temporary.cleanup()
        self.setUp()
        (self.root / ".gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ignore"], check=True)
        self.base = command("git", "-C", str(self.root), "rev-parse", "HEAD")
        target = self._target_bundle({"ignored.txt": "tracked\n"}, force_add=("ignored.txt",))
        (self.root / "ignored.txt").write_text("ignored\n")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "untracked or ignored"):
            self._run()
        self.assertEqual((self.root / "ignored.txt").read_text(), "ignored\n")

    def test_parent_file_ignored_child_and_symlink_conflicts_are_refused(self):
        target = self._target_bundle({"parent/child.txt": "tracked\n"})
        (self.root / "parent").write_text("untracked parent\n")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "parent"):
            self._run()
        self.assertEqual((self.root / "parent").read_text(), "untracked parent\n")

        self.temporary.cleanup()
        self.setUp()
        (self.root / ".gitignore").write_text("ignored-dir/\n")
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ignore directory"], check=True)
        self.base = command("git", "-C", str(self.root), "rev-parse", "HEAD")
        target = self._target_bundle({"ignored-dir/child.txt": "tracked\n"}, force_add=("ignored-dir/child.txt",))
        (self.root / "ignored-dir").mkdir()
        (self.root / "ignored-dir/local.txt").write_text("ignored child\n")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "ignored-dir"):
            self._run()

        self.temporary.cleanup()
        self.setUp()
        target = self._target_bundle({"link-parent/child.txt": "tracked\n"})
        (self.root / "link-parent").symlink_to("not-a-directory")
        self._write_job(target=target)
        with self.assertRaisesRegex(remote_worker.WorkerError, "link-parent"):
            self._run()

    def test_detached_head_sync_keeps_branch_refs_and_enters_syncing_state(self):
        target = self._target_bundle({"tracked.txt": "target\n"})
        branch_before = command("git", "-C", str(self.root), "show-ref", "--heads")
        subprocess.run(["git", "-C", str(self.root), "checkout", "-q", "--detach"], check=True)
        self._write_job(target=target)
        original_sync = remote_worker._sync_baseline

        def check_sync(*args):
            self.assertEqual(json.loads((self.job_dir / "state.json").read_text())["state"], "syncing")
            return original_sync(*args)

        with mock.patch.object(remote_worker.sys, "platform", "linux"), mock.patch.object(remote_worker, "_sync_baseline", side_effect=check_sync):
            manifest = remote_worker.run_job(self.job_dir)
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), target)
        self.assertEqual(command("git", "-C", str(self.root), "show-ref", "--heads"), branch_before)
        self.assertEqual(manifest["source"]["observed_build_base"]["head"], target)
        receipt = json.loads((self.job_dir / "sync.json").read_text())
        self.assertTrue(receipt["detached_head"])
        self.assertIsNone(receipt["branch"])

    def test_local_requires_sync_and_remote_rejects_one(self):
        snapshot = remote_worker.inspect_source(self.root)
        base = {"expected_head": snapshot["head"], "expected_dirty_digest": snapshot["dirty_digest"], "files": []}
        with self.assertRaisesRegex(remote_worker.WorkerError, "local source"):
            remote_worker._source_expected({"source": {"mode": "local", **base}})
        sync = {"strategy": "ff-only", "base_head": self.base, "target_head": self.base, "bundle_sha256": None}
        with self.assertRaisesRegex(remote_worker.WorkerError, "remote source"):
            remote_worker._source_expected({"source": {"mode": "remote", "sync": sync, **base}})

    def test_failed_build_keeps_synced_commit_and_same_head_needs_no_bundle(self):
        target = self._target_bundle({"tracked.txt": "target\n"})
        self._write_job(target=target)
        with mock.patch.object(remote_worker.sys, "platform", "linux"), mock.patch.object(remote_worker, "_run_build", side_effect=remote_worker.WorkerError("build failed")):
            with self.assertRaisesRegex(remote_worker.WorkerError, "build failed"):
                remote_worker.run_job(self.job_dir)
        self.assertEqual(command("git", "-C", str(self.root), "rev-parse", "HEAD"), target)
        self.assertEqual(json.loads((self.job_dir / "sync.json").read_text())["state"], "synced")

        self.job_dir = Path(self.temporary.name) / "same-head"
        self.job_dir.mkdir()
        self.base = target
        self._write_job(target=target)
        manifest = self._run()
        self.assertEqual(manifest["source"]["observed_build_base"]["head"], target)
        self.assertFalse((self.job_dir / "source.bundle").exists())


if __name__ == "__main__":
    unittest.main()
