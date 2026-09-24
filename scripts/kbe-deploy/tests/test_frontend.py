import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kbe_deploy as cli
import device


class FrontendTests(unittest.TestCase):
    def test_missing_manifest_reports_phase_before_any_device_access(self):
        for remote, local, expected in [('failed', 'failed', '远端编译/收集失败'),
                ('succeeded', 'failed', './deploy.sh resume'),
                ('running', 'running', './deploy.sh status'),
                (None, 'queued', './deploy.sh status')]:
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                cli.save_json(root / 'job.json', {'id': 'test-job'})
                cli.save_json(root / 'state.json', {'state': local,
                        'remote_state': {'state': remote}})
                with mock.patch.object(device, 'Device') as adb:
                    with self.assertRaisesRegex(cli.DeployError, expected):
                        device.deploy(root, 'serial')
                    adb.assert_not_called()
                with mock.patch.object(cli, 'jobdir', return_value=root), \
                        mock.patch.object(cli, 'config', return_value={'platforms': {}}), \
                        mock.patch.object(cli, 'list_devices') as devices, \
                        mock.patch('builtins.input', side_effect=['4', 'test-job']) as prompt, \
                        mock.patch('builtins.print'):
                    with self.assertRaisesRegex(cli.DeployError, expected):
                        cli.menu()
                    self.assertEqual(prompt.call_count, 2)
                    devices.assert_not_called()

    def test_existing_bundle_survives_failed_deployment_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            cli.save_json(root / 'state.json', {'state': 'failed',
                    'remote_state': {'state': 'succeeded'}})
            self.assertEqual(cli.verify_bundle(root)[1]['provenance'], 'built')

    def test_legacy_policy_correction_is_narrow_and_audited(self):
        old = {'path': 'system/lib/libaudiopolicyservice.so', 'sha256': 'original'}
        sibling = {'path': 'system/lib64/libaudiopolicyservice.so'}
        artifacts = [old, sibling]
        job = {'platform': {'id': 'rk3568'},
               'modules': [{'id': 'audio-framework', 'artifacts': artifacts}]}
        d = mock.Mock()
        d.metadata.side_effect = [cli.DeployError('missing'), {}]
        d.missing_state.return_value = 'absent'
        d.shell.return_value = '127 69 76 70 2'
        retained, changes = device.correct_legacy_rk3568_artifacts(job, artifacts, d)
        self.assertEqual(retained, [sibling])
        self.assertEqual(changes[0]['artifact'], old)
        self.assertEqual(artifacts, [old, sibling])  # frozen evidence not mutated
        d.metadata.side_effect = None
        self.assertEqual(device.correct_legacy_rk3568_artifacts(job, artifacts, d), (artifacts, []))
        job['platform']['id'] = 'rk3576'
        d.reset_mock()
        self.assertEqual(device.correct_legacy_rk3568_artifacts(job, artifacts, d), (artifacts, []))
        d.metadata.assert_not_called()

    def test_legacy_policy_correction_rejects_ambiguous_absence_or_wrong_server(self):
        artifacts = [{'path': 'system/lib/libaudiopolicyservice.so'},
                     {'path': 'system/lib64/libaudiopolicyservice.so'}]
        job = {'platform': {'id': 'rk3568'},
               'modules': [{'id': 'audio-framework', 'artifacts': artifacts}]}
        for missing in ('whiteout', 'io_error', 'absent'):
            d = mock.Mock()
            d.metadata.side_effect = [cli.DeployError('missing'), {}]
            if missing == 'io_error':
                d.missing_state.side_effect = cli.DeployError('I/O')
            else:
                d.missing_state.return_value = missing
            d.shell.return_value = '127 69 76 70 1'
            with self.assertRaises(cli.DeployError):
                device.correct_legacy_rk3568_artifacts(job, artifacts, d)

    def test_status_distinguishes_build_from_deployment_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli.save_json(root / 'job.json', {'action': 'build'})
            original = {'state': 'failed', 'remote_state': {'state': 'succeeded'},
                        'error': '设备与产物 fingerprint 不同；核实系统基线兼容后可显式使用 --allow-fingerprint-mismatch'}
            cli.save_json(root / 'state.json', original)
            result = cli.status_details(root)
            self.assertEqual(result['state'], 'deploy_blocked')
            self.assertEqual(result['build_state'], 'succeeded')
            self.assertEqual(cli.read_json(root / 'state.json'), original)
            cli.save_json(root / 'job.json', {'action': 'collect'})
            self.assertEqual(cli.status_details(root)['build_state'], 'not_built_collected')
            cli.save_json(root / 'deployment.json', {'state': 'recovery_required'})
            self.assertEqual(cli.status_details(root)['state'], 'failed')
            (root / 'deployment.json').unlink()
            original['error'] = 'download hash mismatch'
            cli.save_json(root / 'state.json', original)
            self.assertEqual(cli.status_details(root)['state'], 'failed')

    def apk_fixture(self, root):
        job, manifest = self.fixture(root)
        name = 'system_ext/priv-app/Engine/Engine.apk'
        apk = root / 'artifacts' / name
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'new-apk')
        job['modules'].append({'id': 'engine', 'package': 'com.test.engine', 'apk_path': name,
                               'artifacts': [{'path': name, 'kind': 'apk'}]})
        manifest['artifacts'].append({'path': name, 'kind': 'apk', 'sha256': cli.digest(apk), 'size': apk.stat().st_size})
        cli.save_json(root / 'job.json', job)
        cli.save_json(root / 'manifest.json', manifest)
        base = self.fake_device()
        class Fake(base):
            active = '/data/app/engine/base.apk'
            pulls = []
            install_failure = False
            def shell(self, *args):
                if args[:2] == ('pm', 'path'):
                    return 'package:' + self.active
                return ''
            def adb(self, action, *args, **kwargs):
                if action == 'install':
                    assert args[0] == '-r'
                    if self.install_failure:
                        return 'Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]'
                    self.files[self.active] = Path(args[1]).read_bytes()
                    return 'Success'
                if action == 'pull':
                    self.pulls.append(args[0])
                return super().adb(action, *args, **kwargs)
        Fake.files['/' + name] = b'system-apk'
        Fake.files[Fake.active] = b'old-data-apk'
        return Fake, name

    def test_data_apk_install_preserves_system_apk_and_has_no_apk_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, name = self.apk_fixture(root)
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'), mock.patch.object(device, 'apk_package', return_value='com.test.engine'):
                device.deploy(root, 'serial')
                self.assertEqual(fake.files[fake.active], b'new-apk')
                self.assertEqual(fake.files['/' + name], b'system-apk')
                self.assertFalse(any(p.endswith('.apk') for p in fake.pulls))
                self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'deployed')
                with self.assertRaisesRegex(cli.DeployError, '不支持自动完整回滚'):
                    device.rollback(root, 'serial')

    def test_install_failure_stops_before_partition_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, name = self.apk_fixture(root)
            fake.install_failure = True
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'), mock.patch.object(device, 'apk_package', return_value='com.test.engine'):
                with self.assertRaisesRegex(cli.DeployError, 'APK 更新失败'):
                    device.deploy(root, 'serial')
            self.assertEqual(fake.files['/system/lib64/libfoo.so'], b'old-binary')
            self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'recovery_required')

    def test_system_apk_uses_partition_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake, name = self.apk_fixture(root)
            fake.active = '/' + name
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'), mock.patch.object(device, 'apk_package', return_value='com.test.engine'):
                device.deploy(root, 'serial')
            self.assertEqual(fake.files['/' + name], b'new-apk')
            self.assertEqual(cli.read_json(root / 'deployment.json')['apk_updates'], [])

    def test_watch_does_not_stop_on_remote_success_before_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli.save_json(root / 'job.json', {'action': 'collect'})
            snapshots = [{'state': 'succeeded'}, {'state': 'downloading'},
                         {'state': 'succeeded', 'acceptance': 'done'}]
            with mock.patch.object(cli, 'status_details', side_effect=snapshots), mock.patch.object(cli, 'worker_alive', return_value=True), mock.patch.object(cli.time, 'sleep') as sleep, mock.patch('builtins.print') as output:
                self.assertEqual(cli.watch(root), 0)
                self.assertEqual(sleep.call_count, 2)
                self.assertTrue(any('未编译、未部署' in str(c) for c in output.call_args_list))

    def test_watch_failure_and_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli.save_json(root / 'job.json', {'action': 'build'})
            with mock.patch.object(cli, 'status_details', return_value={'state': 'deploy_blocked', 'error': 'fingerprint'}), mock.patch.object(cli, 'worker_alive', return_value=False), mock.patch('builtins.print'):
                self.assertEqual(cli.watch(root), 1)
            with mock.patch.object(cli, 'status_details', return_value={'state': 'succeeded'}), mock.patch.object(cli, 'worker_alive', return_value=False), mock.patch('builtins.print'):
                self.assertEqual(cli.watch(root), 1)
            with mock.patch.object(cli, 'status_details', return_value={'state': 'building'}), mock.patch.object(cli, 'worker_alive', return_value=True), mock.patch.object(cli.time, 'sleep', side_effect=KeyboardInterrupt), mock.patch('builtins.print'):
                self.assertEqual(cli.watch(root), 130)

    def test_menu_fingerprint_requires_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            with mock.patch.object(device.Device, 'check_platform', return_value={'ro.build.version.sdk': '34', 'ro.build.fingerprint': 'different'}), mock.patch('builtins.print'), mock.patch('builtins.input', return_value=''):
                with self.assertRaisesRegex(cli.DeployError, '取消'):
                    cli.menu_fingerprint_override(root, 'serial')
            with mock.patch.object(device.Device, 'check_platform', return_value={'ro.build.version.sdk': '34', 'ro.build.fingerprint': 'different'}), mock.patch('builtins.print'), mock.patch('builtins.input', return_value='allow-fingerprint-mismatch'):
                self.assertTrue(cli.menu_fingerprint_override(root, 'serial'))

    def test_script_entry_catches_device_error_and_records_failure(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'jobs' / 'test-job'
            root.mkdir(parents=True)
            cli.save_json(root / 'job.json', {'id': 'test-job'})
            cli.save_json(root / 'state.json', {'state': 'succeeded', 'acceptance': 'old success'})
            code = """import sys, types, runpy
fake = types.ModuleType('device')
def fail(*args):
    from kbe_deploy import DeployError
    raise DeployError('fingerprint blocked fixture')
fake.deploy = fail
fake.rollback = fail
sys.modules['device'] = fake
sys.argv = [sys.argv[1], 'deploy', 'test-job', '--serial', 'serial']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
            result = subprocess.run([sys.executable, '-c', code, str(Path(cli.__file__))], env=dict(os.environ, KBE_DEPLOY_STATE=temp), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('错误: fingerprint blocked fixture', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            state = cli.read_json(root / 'state.json')
            self.assertEqual(state['state'], 'failed')
            self.assertNotIn('acceptance', state)

    def test_failed_metadata_identifies_target(self):
        target = '/system/framework/oat/arm64/services.art'
        with mock.patch.object(device.Device, 'script', side_effect=cli.DeployError('exit=1')):
            with self.assertRaisesRegex(cli.DeployError, 'services.art'):
                device.Device('serial').metadata(target)

    def test_command_error_keeps_shell_argument_and_exit_code(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, '', '')
        with mock.patch.object(cli.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(cli.DeployError, 'services.art'):
                cli.command(['adb', '-s', 'serial', 'shell', 'test -f /system/framework/oat/arm64/services.art'])

    def test_incremental_fingerprint_policy(self):
        left = 'rockchip/rk3576_u/rk3576_u:14/ID/old:userdebug/release-keys'
        right = left.replace('/old:', '/new:')
        self.assertTrue(device.incremental_only(left, right))
        self.assertFalse(device.incremental_only(left, right.replace(':14/', ':15/')))
        self.assertFalse(device.incremental_only(left, right.replace('/ID/', '/OTHER/')))
        manifest = {'product': {'ro.build.version.sdk': '34', 'ro.product.system.device': 'rk3576_u', 'ro.build.fingerprint': left}}
        props = {'ro.build.version.sdk': '34', 'ro.build.fingerprint': right}
        device.validate_identity({'product': 'rk3576_u'}, props, manifest, False)
        props['ro.build.fingerprint'] = right.replace(':14/', ':15/')
        with self.assertRaisesRegex(cli.DeployError, 'release'):
            device.validate_identity({'product': 'rk3576_u'}, props, manifest, True)

    def test_recover_identity_keeps_legacy_manifest_immutable_and_binds_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            platform = {'id': 'fake', 'server': 'host', 'product_out': '/sdk/out/target/product/fake',
                        'product': 'fake', 'device_match': {'properties': {'ro.build.version.sdk': ['34']},
                                                             'abis': ['arm64-v8a']}}
            job = {'id': 'legacy', 'platform': platform}
            fingerprint = 'brand/fake/fake:14/ID/old:userdebug/release-keys'
            manifest = {'schema': 1, 'job_id': 'legacy', 'platform': 'fake',
                        'source': {'observed_build_base': {'head': 'head'}}, 'artifacts': [],
                        'product': {'ro.build.fingerprint': fingerprint}}
            cli.save_json(root / 'job.json', job)
            cli.save_json(root / 'manifest.json', manifest)
            original = (root / 'manifest.json').read_bytes()
            probe = {'fingerprint': fingerprint.replace('/old:', '/new:'), 'fingerprint_sha256': 'a' * 64,
                     'soong_variables_sha256': 'b' * 64,
                     'artifact_sha256': {},
                     'identity': {'ro.build.version.sdk': '34', 'ro.product.system.device': 'fake',
                                  'ro.product.cpu.abilist': 'arm64-v8a,armeabi-v7a'}}
            with mock.patch.object(cli, 'verify_bundle', return_value=(job, manifest)), \
                    mock.patch.object(cli, 'inspect_remote', return_value={'head': 'head', 'dirty': []}), \
                    mock.patch.object(cli, 'ssh', return_value=json.dumps(probe)):
                cli.recover_identity(root)
            self.assertEqual((root / 'manifest.json').read_bytes(), original)
            identity = cli.effective_product_identity(root, manifest)
            self.assertEqual(identity['ro.build.version.sdk'], '34')
            self.assertEqual(identity['ro.product.system.device'], 'fake')
            manifest['product']['ro.build.fingerprint'] = 'changed'
            with self.assertRaisesRegex(cli.DeployError, '侧车证据'):
                cli.effective_product_identity(root, manifest)

    def test_whiteout_art_install_and_rollback_to_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, manifest = self.fixture(root)
            name = 'system/framework/oat/arm64/services.art'
            artifact = root / 'artifacts' / name
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b'art-data')
            job['modules'].append({'id': 'services', 'artifacts': [{'path': name, 'kind': 'art'}]})
            manifest['artifacts'].append({'path': name, 'kind': 'art', 'size': 8, 'sha256': cli.digest(artifact)})
            cli.save_json(root / 'job.json', job)
            cli.save_json(root / 'manifest.json', manifest)
            base = self.fake_device()
            class Fake(base):
                def metadata(self, path):
                    if path == '/' + name and path not in self.files:
                        raise cli.DeployError('missing')
                    return super().metadata(path)
                def missing_state(self, path):
                    if path in self.files:
                        raise cli.DeployError('present')
                    return 'whiteout'
                def shell(self, *args):
                    if args[:2] == ('rm', '-f'):
                        self.files.pop(args[2], None)
                    return ''
                def boot_check(self, platform, artifacts, root, modules):
                    for a in artifacts:
                        if a.get('absent'):
                            self.missing_state('/' + a['path'])
                        else:
                            assert self.sha('/' + a['path']) == a['sha256']
            with mock.patch.object(device, 'Device', Fake), mock.patch.object(device, 'STATE', root / 'state'):
                device.deploy(root, 'serial')
                self.assertEqual(Fake.files['/' + name], b'art-data')
                self.assertFalse((root / 'backup' / name).exists())
                device.rollback(root, 'serial')
                self.assertNotIn('/' + name, Fake.files)
                self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'rolled_back')

    def test_unknown_io_error_is_not_treated_as_absence(self):
        d = device.Device('serial')
        with mock.patch.object(d, 'script', side_effect=['', 'Input/output error']), mock.patch.object(d, 'shell'):
            with self.assertRaisesRegex(cli.DeployError, '拒绝'):
                d.missing_state('/system/framework/oat/arm64/services.art')

    def test_pre_replace_failure_can_retry_with_archived_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            old = {'state': 'failed_before_replace', 'serial': 'serial', 'attempted': [], 'apk_updates': []}
            cli.save_json(root / 'deployment.json', old)
            fake = self.fake_device()
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'):
                device.deploy(root, 'serial')
            history = list((root / 'deployment-history').glob('*/deployment.json'))
            self.assertEqual(len(history), 1)
            self.assertEqual(cli.read_json(history[0]), old)
            self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'deployed')

    def test_reject_unsafe_paths(self):
        for p in ('../foo', '/system/lib/x', 'system/../../x', 'x//y', 'x/./y', 'x\\y', 'x\ny'):
            with self.subTest(p=p), self.assertRaises(cli.DeployError):
                cli.relpath(p)

    def test_no_symlink_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'real').write_text('data')
            (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(cli.DeployError):
                cli.checked_file(root, 'link')

    def fixture(self, root):
        platform = {'id': 'fake', 'product': 'fake_device', 'device_match': {
            'properties': {'ro.board.platform': ['fake'], 'ro.build.version.sdk': ['34']},
            'abis': ['arm64-v8a']}}
        job = {'id': 'test-job', 'platform': platform,
               'modules': [{'id': 'foo', 'artifacts': [{'path': 'system/lib64/libfoo.so', 'kind': 'elf', 'bits': 64}]}]}
        elf = b'\x7fELF\x02\x01' + bytes(12) + b'\xb7\x00' + b'new-binary'
        target = root / 'artifacts/system/lib64/libfoo.so'
        target.parent.mkdir(parents=True)
        target.write_bytes(elf)
        manifest = {'schema': 1, 'job_id': 'test-job', 'platform': 'fake', 'provenance': 'built',
                    'product': {'ro.build.version.sdk': '34', 'ro.product.system.device': 'fake_device', 'ro.build.fingerprint': 'fake/fp'},
                    'artifacts': [{'path': 'system/lib64/libfoo.so', 'kind': 'elf', 'bits': 64,
                                   'sha256': cli.digest(target), 'size': len(elf)}]}
        cli.save_json(root / 'job.json', job)
        cli.save_json(root / 'manifest.json', manifest)
        return job, manifest

    def test_manifest_integrity_arch_and_completeness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, manifest = self.fixture(root)
            self.assertEqual(cli.verify_bundle(root)[0], job)
            path = root / 'artifacts/system/lib64/libfoo.so'
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(cli.DeployError, '哈希'):
                cli.verify_bundle(root)
            manifest['artifacts'] = []
            cli.save_json(root / 'manifest.json', manifest)
            with self.assertRaisesRegex(cli.DeployError, '不完整'):
                cli.verify_bundle(root)

    def test_fingerprint_override_cannot_override_sdk_or_product(self):
        props = {'ro.build.version.sdk': '34', 'ro.build.fingerprint': 'device/fp'}
        product = {'ro.build.version.sdk': '34', 'ro.build.fingerprint': 'build/fp', 'ro.product.system.device': 'rk3576_u'}
        with self.assertRaisesRegex(cli.DeployError, 'fingerprint'):
            device.validate_identity({'product': 'rk3576_u'}, props, {'product': product}, False)
        device.validate_identity({'product': 'rk3576_u'}, props, {'product': product}, True)
        product['ro.build.version.sdk'] = '32'
        with self.assertRaisesRegex(cli.DeployError, 'SDK'):
            device.validate_identity({'product': 'rk3576_u'}, props, {'product': product}, True)

    def test_wrong_device_rejected_before_root(self):
        d = device.Device('serial')
        with mock.patch.object(d, 'adb', return_value='device'), mock.patch.object(d, 'properties', return_value={'ro.board.platform': 'rk356x'}):
            with self.assertRaisesRegex(cli.DeployError, '设备不匹配'):
                d.check_platform({'id': 'rk3576', 'device_match': {'properties': {'ro.board.platform': ['rk3576']}, 'abis': []}})

    def fake_device(self):
        class Fake:
            files = {}
            props = {'ro.build.version.sdk': '34', 'ro.build.fingerprint': 'fake/fp'}
            meta = {'mode': '644', 'uid': '0', 'gid': '0', 'context': 'u:object_r:system_file:s0'}
            replacements = 0
            fail_first = False

            def __init__(self, serial):
                self.serial = serial

            def check_platform(self, p):
                return dict(self.props)

            def root_remount(self):
                pass

            def metadata(self, p):
                return dict(self.meta)

            def sha(self, p):
                return hashlib.sha256(self.files[p]).hexdigest()

            def adb(self, action, source, dest, timeout=60):
                if action == 'pull':
                    Path(dest).write_bytes(self.files[source])
                elif action == 'push':
                    self.files[dest] = Path(source).read_bytes()
                else:
                    raise AssertionError(action)

            def shell(self, *argv):
                return ''

            def replace(self, source, target, meta, token):
                self.files[target] = self.files[source]
                type(self).replacements += 1
                if self.fail_first and self.replacements == 1:
                    raise cli.DeployError('simulated failure after rename')

            def boot_check(self, platform, artifacts, root, modules):
                for a in artifacts:
                    if self.sha('/' + a['path']) != a['sha256']:
                        raise cli.DeployError('boot hash mismatch')
        Fake.files = {'/system/lib64/libfoo.so': b'old-binary'}
        return Fake

    def test_deploy_then_rollback_restores_original_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            fake = self.fake_device()
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'):
                device.deploy(root, 'serial')
                self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'deployed')
                self.assertNotEqual(fake.files['/system/lib64/libfoo.so'], b'old-binary')
                device.rollback(root, 'serial')
                self.assertEqual(fake.files['/system/lib64/libfoo.so'], b'old-binary')
                self.assertEqual(cli.read_json(root / 'deployment.json')['state'], 'rolled_back')

    def test_failure_after_first_rename_automatically_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            fake = self.fake_device()
            fake.fail_first = True
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'):
                with self.assertRaisesRegex(cli.DeployError, '部署未通过'):
                    device.deploy(root, 'serial')
                record = cli.read_json(root / 'deployment.json')
                self.assertEqual(record['state'], 'rolled_back')
                self.assertEqual(fake.files['/system/lib64/libfoo.so'], b'old-binary')
                self.assertEqual(record['attempted'], ['system/lib64/libfoo.so'])

    def test_rollback_refuses_later_changed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root)
            fake = self.fake_device()
            with mock.patch.object(device, 'Device', fake), mock.patch.object(device, 'STATE', root / 'state'):
                device.deploy(root, 'serial')
                fake.files['/system/lib64/libfoo.so'] = b'other-deployment'
                with self.assertRaisesRegex(cli.DeployError, '其他操作'):
                    device.rollback(root, 'serial')
                self.assertEqual(fake.files['/system/lib64/libfoo.so'], b'other-deployment')

    def test_collected_existing_requires_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, manifest = self.fixture(root)
            manifest['provenance'] = 'collected-existing'
            cli.save_json(root / 'manifest.json', manifest)
            with mock.patch.object(device, 'Device') as mock_device:
                with self.assertRaisesRegex(cli.DeployError, 'accept-existing'):
                    device.deploy(root, 'serial')
                mock_device.assert_not_called()


if __name__ == '__main__':
    unittest.main()
