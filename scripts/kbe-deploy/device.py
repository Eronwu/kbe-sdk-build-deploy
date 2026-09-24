"""ADB deployment with a durable before-image journal and explicit device binding."""
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
import re
import shlex
import time

from kbe_deploy import DeployError, STATE, command, digest, effective_product_identity, now, read_json, relpath, save_json, verify_bundle


class Device:
    def __init__(self, serial):
        if not serial or serial.startswith('-') or not re.fullmatch(r'[a-zA-Z0-9_.:-]+', serial):
            raise DeployError('需要明确、有效的 ADB 序列号')
        self.serial = serial

    def adb(self, *args, timeout=60):
        return command(['adb', '-s', self.serial, *args], timeout=timeout)

    def shell(self, *args, timeout=60):
        return self.adb('shell', shlex.join([str(x) for x in args]), timeout=timeout)

    def script(self, script):
        return self.shell('sh', '-c', script)

    def properties(self):
        return dict(re.findall(r'^\[([^]]+)\]: \[(.*)\]$', self.shell('getprop'), re.M))

    def check_platform(self, platform):
        if self.adb('get-state') != 'device':
            raise DeployError('设备不处于 ADB device 状态')
        props = self.properties()
        match = platform['device_match']
        for prop, allowed in match['properties'].items():
            if props.get(prop) not in allowed:
                raise DeployError(f'设备不匹配 {platform["id"]}: {prop}={props.get(prop)!r}，要求 {allowed}')
        if not set(match['abis']).issubset(set(props.get('ro.product.cpu.abilist', '').split(','))):
            raise DeployError('设备 ABI 不匹配')
        if props.get('ro.debuggable') != '1':
            raise DeployError('仅支持已开启调试能力的设备系统分区替换')
        return props

    def root_remount(self):
        self.adb('root')
        self.adb('wait-for-device', timeout=60)
        if self.shell('id', '-u') != '0':
            raise DeployError('adbd 未获得 root；未执行替换')
        message = self.adb('remount', timeout=120)
        if re.search(r'reboot|disable-verity|failed|error|not permitted', message, re.I):
            raise DeployError('remount 尚未就绪，请先处理设备后重试: ' + message)

    def sha(self, path):
        output = self.shell('sha256sum', path).split()
        if not output or not re.fullmatch(r'[a-f0-9]{64}', output[0]):
            raise DeployError('不能读取设备文件哈希: ' + path)
        return output[0]

    def metadata(self, path):
        # Partition files must already exist; adding packages belongs to a separate install flow.
        chain = [str(x) for x in PurePosixPath(path).parents if str(x) != '/'] + [path]
        try:
            self.script(' && '.join('[ ! -L ' + shlex.quote(x) + ' ]' for x in chain) + ' && test -f ' + shlex.quote(path))
        except DeployError as error:
            raise DeployError('无法备份设备原文件（缺失、符号链接或文件系统访问异常）: ' + path + '; ' + str(error)) from error
        stat = self.shell('stat', '-c', '%a %u %g', path).split()
        label = self.shell('ls', '-Zd', path).split()[0]
        if len(stat) != 3 or not all(re.fullmatch(r'[0-9]+', x) for x in stat) or not re.fullmatch(r'u:object_r:[a-zA-Z0-9_]+:s0', label):
            raise DeployError('无法确认权限/SELinux 标签: ' + path)
        return {'mode': stat[0], 'uid': stat[1], 'gid': stat[2], 'context': label}

    def missing_state(self, path):
        """Only accept ENOENT or an evidenced overlay whiteout, never generic I/O errors."""
        chain = [str(x) for x in PurePosixPath(path).parents if str(x) != '/'] + [path]
        self.script(' && '.join('[ ! -L ' + shlex.quote(x) + ' ]' for x in chain) + ' && test -d ' + shlex.quote(str(PurePosixPath(path).parent)))
        # Capture lookup errors without losing their text to a shell test's empty output.
        result = self.script('ls -ld ' + shlex.quote(path) + ' 2>&1; echo __lookup_done__')
        if 'No such file or directory' in result:
            return 'absent'
        if 'Invalid argument' in result:
            for line in self.shell('cat', '/proc/mounts').splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[1] == '/system' and fields[2] == 'overlay':
                    upper = next((x[9:] for x in fields[3].split(',') if x.startswith('upperdir=')), None)
                    if upper and upper.startswith('/') and path.startswith('/system/'):
                        candidate = upper + path[len('/system'):]
                        info = self.shell('stat', '-c', '%F %t:%T', candidate)
                        if info in ('character special file 0:0', 'character device 0:0'):
                            return 'whiteout'
        raise DeployError('文件不是可确认的缺失/whiteout，拒绝当作新文件: ' + path + '; ' + result)

    def replace(self, source, target, meta, token):
        tmp = target + '.kbe-' + token
        # Android is stopped while the entire bundle is installed. Each file is renamed
        # only after bytes, mode and context are ready; the local journal precedes this call.
        self.script('set -e; ' + '; '.join([
            shlex.join(['cp', source, tmp]),
            shlex.join(['chmod', meta['mode'], tmp]),
            shlex.join(['chown', meta['uid'] + ':' + meta['gid'], tmp]),
            shlex.join(['chcon', meta['context'], tmp]),
            shlex.join(['mv', '-f', tmp, target]),
            shlex.join(['restorecon', target]),
        ]))

    def boot_check(self, platform, artifacts, job_dir, modules):
        self.adb('reboot')
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                if self.shell('getprop', 'sys.boot_completed', timeout=10) == '1':
                    break
            except Exception:
                pass
            time.sleep(5)
        else:
            raise DeployError('重启后 240 秒未完成启动')
        self.check_platform(platform)
        health = {}
        for process in ('system_server', 'audioserver'):
            pid = self.shell('pidof', process)
            if not pid:
                raise DeployError('启动后缺少进程: ' + process)
            health[process] = pid
        for a in artifacts:
            if a.get('absent'):
                self.missing_state('/' + a['path'])
                continue
            if self.sha('/' + a['path']) != a['sha256']:
                raise DeployError('重启后哈希不符: ' + a['path'])
            self.metadata('/' + a['path'])
        for module in modules:
            package = module.get('package')
            if package:
                actual = self.shell('pm', 'path', package)
                if 'package:/' + module['apk_path'] not in actual.splitlines():
                    raise DeployError('APK 实际加载路径不符: ' + actual)
                if module.get('process_required') and not self.shell('pidof', package):
                    raise DeployError('服务进程未启动: ' + package)
                health[package] = actual
        crashes = self.shell('logcat', '-b', 'crash', '-d', '-v', 'brief')
        (job_dir / 'post-boot-crash.log').write_text(crashes + '\n')
        if re.search(r'FATAL EXCEPTION|Fatal signal|>>>\s*(system_server|audioserver)', crashes):
            raise DeployError('重启后 crash buffer 存在崩溃，请检查 post-boot-crash.log')
        save_json(job_dir / 'health.json', {'checked_at': now(), 'processes': health, 'acceptance': '部署检查通过；真实播放待设备验收'})


@contextlib.contextmanager
def device_lock(serial):
    directory = STATE / 'locks'
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(serial.encode()).hexdigest()
    with (directory / (key + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('该设备已有部署/回滚任务')
        yield


def fingerprint_baseline(value):
    # Ignore only incremental build number; retain release, build ID, type and tags.
    match = re.fullmatch(r'([^/:]+/[^/:]+/[^/:]+:[^/:]+/[^/:]+)/[^/:]+(:[^/:]+/[^/:]+)', value or '')
    return (match.group(1), match.group(2)) if match else None


def incremental_only(left, right):
    baseline = fingerprint_baseline(left)
    return baseline is not None and baseline == fingerprint_baseline(right)


def validate_identity(platform, props, manifest, allow_fingerprint_mismatch):
    product = manifest.get('product', {})
    sdk = product.get('ro.build.version.sdk')
    if sdk != props.get('ro.build.version.sdk'):
        raise DeployError('产物 Android SDK 与设备不匹配或缺少构建身份')
    devices = {v for k, v in product.items() if k.startswith('ro.product.') and k.endswith('.device')}
    if platform['product'] not in devices:
        raise DeployError('产物 product 与配置不匹配')
    built_abis = set(product.get('ro.product.cpu.abilist', '').split(',')) - {''}
    actual_abis = set(props.get('ro.product.cpu.abilist', '').split(',')) - {''}
    if built_abis and not built_abis.issubset(actual_abis):
        raise DeployError('产物 ABI 与设备不匹配')
    fingerprint = product.get('ro.build.fingerprint')
    if not fingerprint:
        raise DeployError('产物缺少 build fingerprint')
    actual = props.get('ro.build.fingerprint')
    left, right = fingerprint_baseline(fingerprint), fingerprint_baseline(actual)
    if left and right and left[0].split(':')[1].split('/')[0] != right[0].split(':')[1].split('/')[0]:
        raise DeployError('产物与设备 Android release 不匹配')
    if fingerprint != actual and incremental_only(fingerprint, actual):
        print('提示：fingerprint 仅增量构建编号不同，按同基线继续部署。', flush=True)
        return
    if fingerprint != actual and not allow_fingerprint_mismatch:
        raise DeployError('设备与产物 fingerprint 不同；核实系统基线兼容后可显式使用 --allow-fingerprint-mismatch')


def apk_package(apk):
    candidates = [shutil.which('aapt2'), shutil.which('aapt')]
    for key in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(key):
            candidates.extend(str(p) for p in sorted((Path(os.environ[key]) / 'build-tools').glob('*/aapt2'), reverse=True))
    tool = next((p for p in candidates if p and Path(p).is_file()), None)
    if not tool:
        raise DeployError('APK 包名校验需要 aapt2/aapt；请设置 ANDROID_HOME 或 PATH')
    output = command([tool, 'dump', 'badging', str(apk)])
    match = re.search(r"^package: name='([^']+)'", output, re.M)
    if not match:
        raise DeployError('无法读取 APK 包名')
    return match.group(1)


def active_apk(device, module):
    lines = device.shell('pm', 'path', module['package']).splitlines()
    if len(lines) != 1 or not lines[0].startswith('package:/'):
        raise DeployError('APK 路径缺失或为 split APK，不支持单 APK 替换: ' + str(lines))
    actual = lines[0][len('package:'):]
    if actual != '/' + module['apk_path'] and not (actual.startswith('/data/app/') and actual.endswith('.apk')):
        raise DeployError('APK 安装位置不符: ' + actual)
    return actual


def _save(path, record, phase):
    record.update(state=phase, updated_at=now())
    save_json(path / 'deployment.json', record)


def _restore(device, path, job, record):
    if any(a.get('attempted') for a in record.get('apk_updates', [])):
        raise DeployError('/data/app APK 未备份，不能自动恢复完整配套版本；需人工恢复 Engine 和系统库')
    entries = [e for e in record['files'] if e['path'] in record['attempted']]
    # Validate every backup/current file before restoring any. Never silently overwrite
    # a later deployment or another writer's bytes.
    for e in entries:
        if e.get('before_state') in ('absent', 'whiteout'):
            try:
                device.missing_state('/' + e['path'])
            except DeployError:
                if device.sha('/' + e['path']) != e['after_sha256']:
                    raise DeployError('新增文件已被其他操作改变: ' + e['path'])
            continue
        backup = path / 'backup' / e['path']
        if not backup.is_file() or digest(backup) != e['before_sha256']:
            raise DeployError('回滚备份缺失/损坏: ' + e['path'])
        current = device.sha('/' + e['path'])
        if current not in (e['before_sha256'], e['after_sha256']):
            raise DeployError('设备文件已被其他操作改变，拒绝覆盖: ' + e['path'])
    staging = record['staging'] + '/rollback'
    device.shell('mkdir', '-p', staging)
    for i, e in enumerate(entries):
        if e.get('before_state') in ('absent', 'whiteout'):
            continue
        device.adb('push', str(path / 'backup' / e['path']), staging + '/' + str(i), timeout=180)
        if device.sha(staging + '/' + str(i)) != e['before_sha256']:
            raise DeployError('回滚上传校验失败')
    device.shell('stop')
    for i, e in reversed(list(enumerate(entries))):
        if e.get('before_state') in ('absent', 'whiteout'):
            device.shell('rm', '-f', '/' + e['path'])
            device.missing_state('/' + e['path'])
            continue
        device.replace(staging + '/' + str(i), '/' + e['path'], e['metadata'], job['id'])
        if device.sha('/' + e['path']) != e['before_sha256']:
            raise DeployError('回滚哈希不符: ' + e['path'])
    device.shell('sync')
    device.boot_check(job['platform'], [{'path': e['path'], 'sha256': e['before_sha256'], 'absent': e.get('before_state') in ('absent', 'whiteout')} for e in entries], path, [])
    _save(path, record, 'rolled_back')


def correct_legacy_rk3568_artifacts(job, artifacts, device):
    """Narrow migration of the historical server-library list; never alter build evidence."""
    obsolete = 'system/lib/libaudiopolicyservice.so'
    retained = list(artifacts)
    corrections = []
    if job['platform']['id'] != 'rk3568':
        return retained, corrections
    configured = any(m['id'] == 'audio-framework' and
                     any(a['path'] == obsolete for a in m['artifacts']) for m in job['modules'])
    if not configured or not any(a['path'] == obsolete for a in retained):
        return retained, corrections
    try:
        device.metadata('/' + obsolete)
        return retained, corrections  # Existing device variants still receive their full bundle.
    except DeployError:
        if device.missing_state('/' + obsolete) != 'absent':
            raise DeployError('旧清单修正仅支持确认的 ENOENT，不支持 whiteout')
    sibling = 'system/lib64/libaudiopolicyservice.so'
    if not any(a['path'] == sibling for a in retained):
        raise DeployError('旧清单修正要求完整的 64 位 policy service 产物')
    device.metadata('/' + sibling)
    header = device.shell('od', '-An', '-tu1', '-N5', '/system/bin/audioserver').split()
    if header != ['127', '69', '76', '70', '2']:
        raise DeployError('旧清单修正要求设备 audioserver 为 64 位 ELF')
    removed = next(a for a in retained if a['path'] == obsolete)
    corrections.append({'rule': 'rk3568-policy-service-64bit-only-v1',
                        'artifact': dict(removed), 'before_state': 'absent',
                        'evidence': {'audioserver_elf_class': 64, 'present_sibling': sibling}})
    print('提示：修正 RK3568 旧部署清单，排除设备未安装的 ' + obsolete + '；原始 manifest 保留。')
    return [a for a in retained if a['path'] != obsolete], corrections


def deploy(path, serial, allow_fingerprint_mismatch=False, accept_existing=False):
    path = Path(path)
    job, manifest = verify_bundle(path)
    manifest = {**manifest, 'product': effective_product_identity(path, manifest)}
    if manifest['provenance'] == 'collected-existing' and not accept_existing:
        raise DeployError('已有产物无源码构建证明，明确接受后使用 --accept-existing')
    with device_lock(serial):
        for other in (STATE / 'jobs').glob('*/deployment.json'):
            previous = read_json(other)
            if previous.get('serial') == serial and previous.get('state') in ('backing_up', 'deploying', 'rebooting', 'recovery_required'):
                raise DeployError('该设备存在未完成部署，先检查/回滚任务: ' + previous['job_id'])
        if (path / 'deployment.json').exists():
            old = read_json(path / 'deployment.json')
            if (old.get('state') == 'failed_before_replace' and old.get('attempted') == []
                    and not any(a.get('attempted') for a in old.get('apk_updates', []))
                    and old.get('serial') == serial):
                archive = path / 'deployment-history' / str(time.time_ns())
                archive.mkdir(parents=True)
                (path / 'deployment.json').rename(archive / 'deployment.json')
                if (path / 'backup').exists():
                    (path / 'backup').rename(archive / 'backup')
            else:
                raise DeployError('此任务已有部署记录 (' + old['state'] + ')，不重复覆盖；回滚用 rollback')
        device = Device(serial)
        props = device.check_platform(job['platform'])
        validate_identity(job['platform'], props, manifest, allow_fingerprint_mismatch)
        packages = {m['package']: m for m in job['modules'] if m.get('package')}
        routes = {}
        for package, module in packages.items():
            routes[package] = active_apk(device, module)
            if apk_package(path / 'artifacts' / module['apk_path']) != package:
                raise DeployError('新 APK 包名与目标不一致: ' + package)
        device.root_remount()
        # Re-check identity after reconnect/root, before any backup or mutation.
        props = device.check_platform(job['platform'])
        validate_identity(job['platform'], props, manifest, allow_fingerprint_mismatch)
        for package, module in packages.items():
            if active_apk(device, module) != routes[package]:
                raise DeployError('重连后 APK 生效路径改变，停止部署')
        updates = [{'package': package, 'path': packages[package]['apk_path'], 'before_path': actual,
                    'attempted': False, 'backup_available': False}
                   for package, actual in routes.items() if actual.startswith('/data/app/')]
        update_paths = {a['path'] for a in updates}
        partition_artifacts = [a for a in manifest['artifacts'] if a['path'] not in update_paths]
        record = {'schema': 1, 'job_id': job['id'], 'serial': serial, 'platform': job['platform']['id'],
                  'fingerprint': props['ro.build.fingerprint'], 'files': [], 'attempted': [], 'apk_updates': updates,
                  'staging': '/data/local/tmp/kbe-deploy-' + job['id'], 'created_at': now()}
        _save(path, record, 'backing_up')
        stopped = False
        try:
            partition_artifacts, corrections = correct_legacy_rk3568_artifacts(
                    job, partition_artifacts, device)
            record['artifact_corrections'] = corrections
            _save(path, record, 'backing_up')
            device.shell('mkdir', '-p', record['staging'])
            for i, a in enumerate(partition_artifacts):
                name = relpath(a['path'])
                target = '/' + name
                before_state = 'present'
                try:
                    meta = device.metadata(target)
                except DeployError:
                    # Only the configured services ART siblings may be newly installed.
                    allowed = any(m['id'] == 'services' and any(x['path'] == name for x in m['artifacts']) for m in job['modules'])
                    if not allowed or not re.fullmatch(r'system/framework/oat/(arm|arm64)/services\.(art|odex|vdex)', name):
                        raise
                    before_state = device.missing_state(target)
                    device.metadata('/system/framework/services.jar')
                    meta = {'mode': '644', 'uid': '0', 'gid': '0', 'context': 'u:object_r:system_file:s0'}
                before = None
                if before_state == 'present':
                    before = device.sha(target)
                    backup = path / 'backup' / name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    device.adb('pull', target, str(backup), timeout=180)
                    if digest(backup) != before:
                        raise DeployError('备份哈希不符: ' + name)
                device.adb('push', str(path / 'artifacts' / name), record['staging'] + '/' + str(i), timeout=180)
                if device.sha(record['staging'] + '/' + str(i)) != a['sha256']:
                    raise DeployError('上传哈希不符: ' + name)
                record['files'].append({'path': name, 'before_state': before_state, 'before_sha256': before, 'after_sha256': a['sha256'], 'metadata': meta})
                _save(path, record, 'backing_up')
            for e in record['files']:
                if e.get('before_state') in ('absent', 'whiteout'):
                    device.missing_state('/' + e['path'])
                    continue
                if device.sha('/' + e['path']) != e['before_sha256']:
                    raise DeployError('备份期间设备文件发生变化: ' + e['path'])
            # PackageManager must still be running; install before stopping Android.
            # Journal before invoking adb: a transport error can hide a committed install.
            for update in updates:
                if active_apk(device, packages[update['package']]) != update['before_path']:
                    raise DeployError('安装前 APK 生效路径改变')
                update['attempted'] = True
                _save(path, record, 'deploying')
                output = device.adb('install', '-r', str(path / 'artifacts' / update['path']), timeout=180)
                if 'Success' not in output.splitlines():
                    raise DeployError('APK 更新失败: ' + output)
                actual = active_apk(device, packages[update['package']])
                expected = next(a['sha256'] for a in manifest['artifacts'] if a['path'] == update['path'])
                if not actual.startswith('/data/app/') or device.sha(actual) != expected:
                    raise DeployError('更新后 APK 实际路径/哈希不符')
                update.update(after_path=actual, sha256=expected, installed=True)
                _save(path, record, 'deploying')
            _save(path, record, 'deploying')
            device.shell('stop')
            stopped = True
            for i, e in enumerate(record['files']):
                record['attempted'].append(e['path'])
                _save(path, record, 'deploying')
                device.replace(record['staging'] + '/' + str(i), '/' + e['path'], e['metadata'], job['id'])
                if device.sha('/' + e['path']) != e['after_sha256']:
                    raise DeployError('替换后哈希不符: ' + e['path'])
                if device.metadata('/' + e['path']) != e['metadata']:
                    raise DeployError('替换后权限/标签不符: ' + e['path'])
            device.shell('sync')
            _save(path, record, 'rebooting')
            checked_artifacts = partition_artifacts + [{'path': a['after_path'].lstrip('/'), 'sha256': a['sha256']} for a in updates]
            checked_modules = [dict(m, apk_path=next((a['after_path'].lstrip('/') for a in updates if a['package'] == m.get('package')), m.get('apk_path'))) for m in job['modules']]
            device.boot_check(job['platform'], checked_artifacts, path, checked_modules)
            _save(path, record, 'deployed')
        except Exception as failure:
            record['error'] = str(failure)
            if any(a.get('attempted') for a in updates):
                if stopped:
                    try:
                        device.shell('start')
                    except Exception as restart_error:
                        record['restart_error'] = str(restart_error)
                _save(path, record, 'recovery_required')
                raise DeployError('部署失败；/data/app APK 安装已尝试且未备份，需核实当前版本后恢复完整配套: ' + str(failure)) from failure
            if record['attempted']:
                try:
                    # A reconnect may reset adbd privileges. Recovery is attempted once.
                    if device.check_platform(job['platform']).get('ro.build.fingerprint') != record['fingerprint']:
                        raise DeployError('回滚时设备系统身份变化')
                    device.root_remount()
                    _restore(device, path, job, record)
                except Exception as rollback_error:
                    record['rollback_error'] = str(rollback_error)
                    _save(path, record, 'recovery_required')
                    raise DeployError(f'部署失败，自动回滚未完成: {failure}; {rollback_error}; 备份: {path / "backup"}') from failure
            else:
                if stopped:
                    device.shell('start')
                _save(path, record, 'failed_before_replace')
            raise DeployError('部署未通过: ' + str(failure) + '; 状态=' + record['state']) from failure


def rollback(path, serial):
    path = Path(path)
    job = read_json(path / 'job.json')
    record = read_json(path / 'deployment.json')
    if record['serial'] != serial or record['job_id'] != job['id']:
        raise DeployError('回滚必须使用原设备和原任务')
    if record['state'] == 'rolled_back':
        raise DeployError('此任务已经回滚')
    if any(a.get('attempted') for a in record.get('apk_updates', [])):
        raise DeployError('/data/app APK 未备份，此任务不支持自动完整回滚')
    if not record['attempted']:
        raise DeployError('此任务没有需要回滚的替换')
    with device_lock(serial):
        device = Device(serial)
        props = device.check_platform(job['platform'])
        if props.get('ro.build.fingerprint') != record['fingerprint']:
            raise DeployError('设备系统基线已改变，不能应用旧备份')
        device.root_remount()
        try:
            _restore(device, path, job, record)
        except Exception as e:
            record['rollback_error'] = str(e)
            _save(path, record, 'recovery_required')
            raise
