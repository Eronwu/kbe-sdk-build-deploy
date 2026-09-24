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
SPEC = importlib.util.spec_from_file_location("remote_worker", WORKER)
remote_worker = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = remote_worker
SPEC.loader.exec_module(remote_worker)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RemoteWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sdk"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        (self.root / "tracked.txt").write_text("base\n")
        build = self.root / "build"
        build.mkdir()
        (build / "envsetup.sh").write_text(
            "lunch() { export OUT=\"$PWD/out\"; }\n"
            "m() { mkdir -p \"$OUT/system/lib64\"; printf built > \"$OUT/system/lib64/libfoo.so\"; }\n"
        )
        product = self.root / "out"
        (product / "system").mkdir(parents=True)
        (product / "system/build.prop").write_text(
            "ro.product.system.device=fake_device\nro.product.system.name=fake_name\n"
            "ro.build.version.sdk=30\nro.build.fingerprint=fake/fingerprint:30\n"
        )
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        self.job_dir = Path(self.temporary.name) / "job"
        self.job_dir.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_job(self, source):
        before = remote_worker.inspect_source(self.root)
        job = {
            "id": "job-1",
            "action": "build",
            "platform": {"id": "fake", "remote_root": str(self.root), "lunch": "fake-userdebug", "product_out": "out"},
            "modules": [{"id": "foo", "targets": ["fake_target"], "artifacts": [{"path": "system/lib64/libfoo.so", "kind": "elf", "bits": 64}]}],
            "jobs": 1,
            "source": {"expected_head": before["head"], "expected_dirty_digest": before["dirty_digest"], **source},
        }
        (self.job_dir / "job.json").write_text(json.dumps(job))

    def test_inspect_is_tracked_only_and_stable(self):
        first = remote_worker.inspect_source(self.root)
        (self.root / "ignored-untracked").write_text("ignored")
        self.assertEqual(first, remote_worker.inspect_source(self.root))
        (self.root / "tracked.txt").write_text("changed\n")
        changed = remote_worker.inspect_source(self.root)
        self.assertNotEqual(first["dirty_digest"], changed["dirty_digest"])
        self.assertEqual(changed["dirty"][0]["path"], "tracked.txt")

    def test_standard_android_envsetup_symlink_is_supported(self):
        original = self.root / 'build/envsetup.sh'
        real = self.root / 'build/make/envsetup.sh'
        real.parent.mkdir()
        original.rename(real)
        original.symlink_to('make/envsetup.sh')
        self._write_job({'mode': 'remote', 'files': []})
        with mock.patch.object(remote_worker.sys, 'platform', 'linux'):
            manifest = remote_worker.run_job(self.job_dir)
        self.assertEqual(manifest['provenance'], 'built')

    def test_product_props_standard_etc_layout(self):
        product = self.root / 'out/product/etc'
        product.mkdir(parents=True)
        (product / 'build.prop').write_text('ro.product.product.device=fake_device\n')
        self.assertEqual(remote_worker._product_props(self.root / 'out')['ro.product.product.device'], 'fake_device')

    def test_product_props_falls_back_to_soong_identity(self):
        product = self.root / 'out/target/product/fake_device'
        product.mkdir(parents=True)
        (product / 'build_fingerprint.txt').write_text('fake/fingerprint')
        soong = self.root / 'out/soong'
        soong.mkdir()
        (soong / 'soong.variables').write_text(json.dumps({
            'Platform_sdk_version': 34, 'DeviceName': 'fake_device',
            'DeviceAbi': ['arm64-v8a'], 'DeviceSecondaryAbi': ['armeabi-v7a']}))
        self.assertEqual(remote_worker._product_props(product), {
            'ro.build.fingerprint': 'fake/fingerprint', 'ro.build.version.sdk': '34',
            'ro.product.cpu.abilist': 'arm64-v8a,armeabi-v7a',
            'ro.product.system.device': 'fake_device'})

    def test_build_stages_manifest_and_restores_overlay(self):
        overlay_bytes = b"temporary overlay\n"
        overlay = self.job_dir / "overlay"
        overlay.mkdir()
        (overlay / "tracked.txt").write_bytes(overlay_bytes)
        self._write_job({"mode": "overlay", "files": [{"path": "tracked.txt", "sha256": sha256(overlay_bytes), "baseline_sha256": sha256(b"base\n")} ]})
        with mock.patch.object(remote_worker.sys, "platform", "linux"):
            manifest = remote_worker.run_job(self.job_dir)
        self.assertEqual((self.root / "tracked.txt").read_text(), "base\n")
        self.assertFalse((self.root / ".kbe-deploy.transaction.json").exists())
        self.assertEqual(manifest["modules"], ["foo"])
        self.assertEqual(manifest["product"]["ro.build.fingerprint"], "fake/fingerprint:30")
        staged = self.job_dir / "artifacts/system/lib64/libfoo.so"
        self.assertEqual(staged.read_bytes(), b"built")
        self.assertEqual(json.loads((self.job_dir / "state.json").read_text())["state"], "succeeded")

    def test_overlay_rejects_undeclared_bytes(self):
        overlay = self.job_dir / "overlay"
        overlay.mkdir()
        (overlay / "tracked.txt").write_bytes(b"x")
        (overlay / "extra.txt").write_bytes(b"x")
        self._write_job({"mode": "overlay", "files": [{"path": "tracked.txt", "sha256": sha256(b"x"), "baseline_sha256": sha256(b"base\n")} ]})
        with self.assertRaisesRegex(remote_worker.WorkerError, "not explicitly declared"):
            remote_worker._validate_overlay(self.job_dir, self.root, json.loads((self.job_dir / "job.json").read_text())["source"]["files"])

    def test_duplicate_artifacts_deduplicate_only_when_compatible(self):
        first = {"path": "system/lib64/libfoo.so", "kind": "elf", "bits": 64}
        duplicate = dict(first)
        job = {"modules": [{"artifacts": [first]}, {"artifacts": [duplicate]}]}
        self.assertEqual(remote_worker._artifact_specs(job), [first])
        duplicate["kind"] = "jar"
        with self.assertRaisesRegex(remote_worker.WorkerError, "conflicting"):
            remote_worker._artifact_specs(job)

    def test_build_targets_accept_relative_art_paths_and_reject_traversal(self):
        job = {"modules": [{"targets": [
            "services",
            "out/target/product/rk3576_u/system/framework/oat/arm64/services.art",
        ]}]}
        self.assertEqual(remote_worker._targets(job), job["modules"][0]["targets"])
        for unsafe in ("/absolute/services.art", "../services.art", "out/../services.art",
                       "out/services art", "out/services;rm"):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                    remote_worker.WorkerError, "unsafe build target"):
                remote_worker._targets({"modules": [{"targets": [unsafe]}]})

    def test_partial_overlay_copy_failure_restores_from_journal(self):
        (self.root / "second.txt").write_text("second base\n")
        subprocess.run(["git", "-C", str(self.root), "add", "second.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "second"], check=True)
        overlay = self.job_dir / "overlay"
        overlay.mkdir()
        (overlay / "tracked.txt").write_text("overlay first\n")
        (overlay / "second.txt").write_text("overlay second\n")
        self._write_job({"mode": "overlay", "files": [
            {"path": "tracked.txt", "sha256": sha256(b"overlay first\n"), "baseline_sha256": sha256(b"base\n")},
            {"path": "second.txt", "sha256": sha256(b"overlay second\n"), "baseline_sha256": sha256(b"second base\n")},
        ]})
        original_copy = remote_worker.shutil.copyfile
        failed = [False]

        def fail_second_target(source, destination, *args, **kwargs):
            if Path(destination).resolve() == (self.root / "second.txt").resolve() and not failed[0]:
                failed[0] = True
                raise OSError("simulated partial overlay write")
            return original_copy(source, destination, *args, **kwargs)

        with mock.patch.object(remote_worker.sys, "platform", "linux"), mock.patch.object(remote_worker.shutil, "copyfile", side_effect=fail_second_target):
            with self.assertRaisesRegex(OSError, "partial overlay"):
                remote_worker.run_job(self.job_dir)
        self.assertTrue(failed[0])
        self.assertEqual((self.root / "tracked.txt").read_text(), "base\n")
        self.assertEqual((self.root / "second.txt").read_text(), "second base\n")
        self.assertFalse((self.root / ".kbe-deploy.transaction.json").exists())

    def test_interrupted_build_restores_overlay(self):
        overlay = self.job_dir / "overlay"
        overlay.mkdir()
        (overlay / "tracked.txt").write_text("temporary\n")
        self._write_job({"mode": "overlay", "files": [{"path": "tracked.txt", "sha256": sha256(b"temporary\n"), "baseline_sha256": sha256(b"base\n")} ]})
        with mock.patch.object(remote_worker.sys, "platform", "linux"), mock.patch.object(remote_worker, "_run_build", side_effect=remote_worker.WorkerError("interrupted by signal 15")):
            with self.assertRaisesRegex(remote_worker.WorkerError, "interrupted"):
                remote_worker.run_job(self.job_dir)
        self.assertEqual((self.root / "tracked.txt").read_text(), "base\n")
        self.assertFalse((self.root / ".kbe-deploy.transaction.json").exists())

    def test_product_props_reads_system_ext_fingerprint_fallback(self):
        product = self.root / "alternate-out"
        (product / "system_ext").mkdir(parents=True)
        (product / "product").mkdir(parents=True)
        (product / "system_ext/build.prop").write_text("ro.system.build.fingerprint=system-ext/fingerprint\nro.product.system_ext.device=ext-device\n")
        (product / "product/build.prop").write_text("ro.product.product.device=product-device\n")
        self.assertEqual(remote_worker._product_props(product), {
            "ro.build.fingerprint": "system-ext/fingerprint",
            "ro.product.product.device": "product-device",
            "ro.product.system_ext.device": "ext-device",
        })

    def test_main_records_non_worker_failure(self):
        (self.job_dir / "job.json").write_text(json.dumps({"id": "job-1"}))
        with mock.patch.object(remote_worker, "run_job", side_effect=OSError("disk error")):
            self.assertEqual(remote_worker.main(["run", str(self.job_dir)]), 1)
        state = json.loads((self.job_dir / "state.json").read_text())
        self.assertEqual(state["state"], "failed")
        self.assertIn("disk error", (self.job_dir / "build.log").read_text())


if __name__ == "__main__":
    unittest.main()
