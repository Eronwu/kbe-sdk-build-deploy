import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kbe_deploy as cli
import remote_worker


class SyncFrontendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.local = self.path / 'local'
        self.remote = self.path / 'remote'
        self.git_at(self.path, 'init', '-q', str(self.local))
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.local / 'source.txt').write_text('old\n')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git_at(self.path, 'clone', '-q', str(self.local), str(self.remote))
        (self.local / 'source.txt').write_text('new committed\n')
        self.git('commit', '-qam', 'new commit')
        self.target = self.git('rev-parse', 'HEAD')
        self.platform = {'local_root': str(self.local), 'remote_root': str(self.remote)}
        self.snapshot = remote_worker.inspect_source(self.remote)

    def tearDown(self):
        self.tmp.cleanup()

    def git_at(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE).decode().strip()

    def git(self, *args):
        return self.git_at(self.local, *args)

    def plan(self, mode='local', file_list=None, sync=None):
        with mock.patch.object(cli, 'inspect_remote', return_value=self.snapshot):
            return cli.source_plan(self.platform, mode, file_list, sync)

    def test_plan_is_read_only_and_records_fast_forward(self):
        source = self.plan()
        self.assertEqual(source['sync']['target_head'], self.target)
        self.assertEqual(source['sync']['base_head'], self.base)
        self.assertEqual(source['sync']['commit_count'], 1)
        self.assertEqual(self.git_at(self.remote, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.git('for-each-ref', 'refs/kbe-deploy'), '')

    def test_bundle_uses_frozen_commit_and_cleans_temporary_ref(self):
        source = self.plan()
        (self.local / 'other').write_text('later')
        self.git('add', 'other')
        self.git('commit', '-qm', 'later commit')
        later = self.git('rev-parse', 'HEAD')
        jobdir = self.path / 'job'
        jobdir.mkdir()
        cli.freeze_bundle(jobdir, self.platform, source)
        heads = self.git('bundle', 'list-heads', str(jobdir / 'source.bundle'))
        self.assertTrue(heads.startswith(self.target + ' '))
        self.assertEqual(cli.digest(jobdir / 'source.bundle'), source['sync']['bundle_sha256'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), later)
        self.assertEqual(self.git('for-each-ref', 'refs/kbe-deploy'), '')

    def test_overlay_baseline_is_target_commit_not_old_remote(self):
        (self.local / 'source.txt').write_text('temporary local change\n')
        file_list = self.path / 'files.txt'
        file_list.write_text('source.txt\n')
        source = self.plan('overlay', str(file_list), 'local-head')
        f = source['files'][0]
        self.assertEqual(f['baseline_sha256'], hashlib.sha256(b'new committed\n').hexdigest())
        self.assertEqual(f['sha256'], hashlib.sha256(b'temporary local change\n').hexdigest())
        self.assertEqual((self.remote / 'source.txt').read_text(), 'old\n')

    def test_dirty_remote_stops_before_bundle(self):
        self.snapshot['dirty'] = [{'path': 'source.txt'}]
        with self.assertRaisesRegex(cli.DeployError, '远端有 tracked'):
            self.plan()

    def test_diverged_remote_stops(self):
        self.git('checkout', '--detach', self.base)
        (self.local / 'branch-file').write_text('unrelated remote work')
        self.git('add', 'branch-file')
        self.git('commit', '-qm', 'remote-only')
        diverged = self.git('rev-parse', 'HEAD')
        self.git('checkout', '--detach', self.target)
        self.snapshot['head'] = diverged
        with self.assertRaisesRegex(cli.DeployError, '不能快进'):
            self.plan()

    def test_committed_only_explicitly_excludes_dirty_files(self):
        (self.local / 'source.txt').write_text('dirty')
        with mock.patch.object(cli, 'inspect_remote', return_value=self.snapshot):
            source = cli.source_plan(self.platform, 'local', None, committed_only=True)
        self.assertEqual(source['files'], [])
        self.assertEqual(source['excluded_tracked_changes'], ['source.txt'])
        self.assertTrue(source['committed_only'])
        self.assertEqual((self.local / 'source.txt').read_text(), 'dirty')
        with self.assertRaises(cli.DeployError):
            cli.source_plan(self.platform, 'remote', None, committed_only=True)

    def test_menu_generates_all_or_partial_file_list(self):
        (self.local / 'source.txt').write_text('dirty')
        (self.local / 'new.txt').write_text('new')
        with mock.patch.object(cli, 'STATE', self.path / 'state'), mock.patch('builtins.print'), mock.patch('builtins.input', return_value='1'):
            args = cli.menu_local_changes(self.platform)
        self.assertEqual(cli.read_file_list(args[1]), ['new.txt', 'source.txt'])
        with mock.patch.object(cli, 'STATE', self.path / 'state'), mock.patch('builtins.print'), mock.patch('builtins.input', side_effect=['2', '2']):
            args = cli.menu_local_changes(self.platform)
        self.assertEqual(cli.read_file_list(args[1]), ['source.txt'])

    def test_menu_deleted_file_requires_commit_or_exclusion(self):
        (self.local / 'source.txt').unlink()
        with mock.patch('builtins.print'), mock.patch('builtins.input', return_value='1'):
            with self.assertRaisesRegex(cli.DeployError, '不能纳入'):
                cli.menu_local_changes(self.platform)
        with mock.patch('builtins.print'), mock.patch('builtins.input', return_value='3'):
            self.assertEqual(cli.menu_local_changes(self.platform), ['--committed-only'])

    def test_dirty_local_requires_explicit_files(self):
        (self.local / 'source.txt').write_text('not committed')
        with self.assertRaisesRegex(cli.DeployError, '未提交 tracked'):
            self.plan()

    def test_unrelated_local_changes_are_explicitly_excluded(self):
        (self.local / 'source.txt').write_text('unrelated')
        (self.local / 'new.txt').write_text('selected')
        file_list = self.path / 'files.txt'
        file_list.write_text('new.txt\n')
        source = self.plan('local', str(file_list))
        self.assertEqual(source['excluded_tracked_changes'], ['source.txt'])
        self.assertIsNone(source['files'][0]['baseline_sha256'])

    def test_old_overlay_does_not_implicitly_sync(self):
        file_list = self.path / 'files.txt'
        file_list.write_text('source.txt\n')
        with self.assertRaisesRegex(cli.DeployError, '--sync local-head'):
            self.plan('overlay', str(file_list))

    def test_same_head_needs_no_bundle(self):
        self.snapshot['head'] = self.target
        source = self.plan()
        jobdir = self.path / 'job'
        jobdir.mkdir()
        cli.freeze_bundle(jobdir, self.platform, source)
        self.assertFalse((jobdir / 'source.bundle').exists())
        self.assertIsNone(source['sync']['bundle_sha256'])

    def test_remote_mode_cannot_silently_sync(self):
        with self.assertRaisesRegex(cli.DeployError, '需要 --source'):
            self.plan('remote', sync='local-head')

    def test_status_skips_unsubmitted_preparation_directory(self):
        state = self.path / 'state'
        (state / 'jobs/unfinished-preparation').mkdir(parents=True)
        with mock.patch.object(cli, 'STATE', state):
            self.assertEqual(cli.main(['status']), 0)

    def test_frontend_bundle_to_remote_build_manifest(self):
        # Full protocol test with a shell fixture; no SDK/compiler/device is used.
        build = self.local / 'build'
        build.mkdir()
        (build / 'envsetup.sh').write_text('lunch() { :; }\nm() { mkdir -p out/system/framework; printf fixture > out/system/framework/test.jar; }\n')
        self.git('add', 'build/envsetup.sh')
        self.git('commit', '-qm', 'add fixture runner')
        product = self.remote / 'out'
        (product / 'system').mkdir(parents=True)
        (product / 'system/build.prop').write_text('ro.build.version.sdk=34\nro.product.system.device=fake\nro.build.fingerprint=fake/fp\n')
        source = self.plan()
        jobdir = self.path / 'job'
        jobdir.mkdir()
        cli.freeze_bundle(jobdir, self.platform, source)
        job = {'id': 'end-to-end', 'action': 'build', 'source': source, 'platform': {
            'id': 'fake', 'remote_root': str(self.remote), 'product_out': str(product), 'lunch': 'fake-userdebug'},
            'modules': [{'id': 'fixture', 'targets': ['fixture'], 'artifacts': [{'path': 'system/framework/test.jar', 'kind': 'jar'}]}], 'jobs': 1}
        cli.save_json(jobdir / 'job.json', job)
        with mock.patch.object(remote_worker.sys, 'platform', 'linux'):
            remote_worker.run_job(jobdir)
        actual, manifest = cli.verify_bundle(jobdir)
        self.assertEqual(manifest['source']['observed_build_base']['head'], source['sync']['target_head'])
        self.assertEqual(manifest['source']['observed_before']['head'], self.base)
        self.assertEqual(manifest['provenance'], 'built')
        self.assertEqual(actual['id'], 'end-to-end')


if __name__ == '__main__':
    unittest.main()
