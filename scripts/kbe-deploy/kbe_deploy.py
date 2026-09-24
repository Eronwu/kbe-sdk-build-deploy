#!/usr/bin/env python3
"""KBE build/deploy front end. Python standard library; no local SDK builds."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

# Share the CLI module identity with device.py, including DeployError.
if __name__ == '__main__':
    sys.modules['kbe_deploy'] = sys.modules[__name__]

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('KBE_DEPLOY_STATE', '~/.local/state/kbe-deploy')).expanduser().resolve()
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
TERMINAL = {'succeeded', 'failed', 'recovery_required', 'deployed', 'rolled_back', 'deploy_blocked'}


class DeployError(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with tmp.open('w') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def relpath(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or '..' in p.parts or '.' == value or str(p) != value or '\\' in value or '\n' in value or '\x00' in value:
        raise DeployError('非法相对路径: ' + repr(value))
    return value


def checked_file(base, value):
    relpath(value)
    base = Path(base).resolve()
    p = base / value
    if not p.resolve().is_relative_to(base):
        raise DeployError('路径越界: ' + value)
    for ancestor in [p, *p.parents]:
        if ancestor == base:
            break
        if ancestor.is_symlink():
            raise DeployError('不支持符号链接产物/源码: ' + value)
    if not p.is_file() or p.stat().st_size == 0:
        raise DeployError('文件缺失或为空: ' + str(p))
    return p


def command(argv, timeout=60, input=None, check=True):
    result = subprocess.run([str(x) for x in argv], input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise DeployError('命令失败 (exit=' + str(result.returncode) + '): ' + shlex.join([str(x) for x in argv]) + '\n' + ((result.stderr + '\n' + result.stdout).strip() or '命令未输出错误详情')[-4000:])
    return result.stdout.strip()


def ssh(platform, argv, timeout=60):
    return command(SSH + [platform['server'], shlex.join([str(x) for x in argv])], timeout)


def scp(platform, source, dest, upload=False):
    remote = platform['server'] + ':'
    # All remote destinations are generated paths without shell metacharacters.
    remote_path = str(dest if upload else source)
    if not re.fullmatch(r'/[a-zA-Z0-9_./+-]+', remote_path) or '..' in PurePosixPath(remote_path).parts:
        raise DeployError('SCP 远端路径包含不支持的字符: ' + remote_path)
    if upload:
        source, dest = str(source), remote + str(dest)
    else:
        source, dest = remote + str(source), str(dest)
    command(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', source, dest], timeout=1800)


def config():
    data = read_json(ROOT / 'config' / 'platforms.json')
    if data.get('schema') != 1:
        raise DeployError('不支持的配置格式')
    for key, p in data['platforms'].items():
        if p['id'] != key or not re.fullmatch(r'[a-zA-Z0-9_.@-]+', p['server']) or p['server'].startswith('-'):
            raise DeployError('非法平台配置')
        for field in ('remote_root', 'remote_jobs', 'product_out'):
            if p[field] is None and not p.get('ready', True):
                continue
            if not re.fullmatch(r'/[a-zA-Z0-9_./-]+', p[field]) or '..' in Path(p[field]).parts:
                raise DeployError('远端路径须为明确绝对路径: ' + field)
    return data


def select_modules(platform, value):
    ids = list(dict.fromkeys(value.split(',')))
    result = []
    for name in ids:
        if name not in platform['modules']:
            raise DeployError('平台不支持组件: ' + name)
        item = dict(platform['modules'][name], id=name)
        for artifact in item['artifacts']:
            path = relpath(artifact['path'])
            if path.split('/')[0] not in ('system', 'system_ext', 'vendor', 'product'):
                raise DeployError('不支持的部署分区: ' + path)
        result.append(item)
    if not result:
        raise DeployError('至少选择一个组件')
    return result


def jobdir(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', value):
        raise DeployError('非法任务 ID')
    p = STATE / 'jobs' / value
    if not (p / 'job.json').is_file():
        raise DeployError('任务不存在: ' + value)
    return p


def state(path, phase, **fields):
    old = read_json(path / 'state.json') if (path / 'state.json').exists() else {}
    if phase not in ('failed', 'recovery_required'):
        old.pop('error', None)
        old.pop('detail', None)
    if phase not in ('succeeded', 'deployed', 'rolled_back'):
        old.pop('acceptance', None)
    old.update(state=phase, updated_at=now(), **fields)
    save_json(path / 'state.json', old)


def inspect_remote(platform):
    # Execute the read-only inspector without installing anything on the server.
    ssh(platform, ['python3', '-c', 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'])
    code = (ROOT / 'remote_worker.py').read_text()
    return json.loads(ssh(platform, ['python3', '-c', code, 'inspect', platform['remote_root']], timeout=120))


def read_file_list(file):
    values = [relpath(x.strip()) for x in Path(file).read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
    if not values or len(values) != len(set(values)):
        raise DeployError('源码清单为空或包含重复路径')
    for value in values:
        if any(part in ('.git', 'out', '.repo') for part in PurePosixPath(value).parts):
            raise DeployError('禁止覆盖构建输出/版本控制目录: ' + value)
    return values


def committed_file_sha(root, commit, name):
    """Hash the blob at the build baseline, never the mutable local working copy."""
    raw = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-z', commit, '--', name])
    if not raw:
        return None
    meta, actual = raw.rstrip(b'\0').split(b'\t', 1)
    permission, kind, oid = meta.split()
    if actual.decode() != name or kind != b'blob' or permission not in (b'100644', b'100755'):
        raise DeployError('覆盖清单包含非普通 Git 文件: ' + name)
    blob = subprocess.check_output(['git', '-C', str(root), 'cat-file', 'blob', oid.decode()])
    return hashlib.sha256(blob).hexdigest()


def source_plan(platform, mode, file_list, sync=None, committed_only=False):
    if committed_only and (mode != 'local' or file_list):
        raise DeployError('--committed-only 仅可搭配 --source local，不能同时指定文件清单')
    remote = inspect_remote(platform)
    source = {'mode': mode, 'expected_head': remote['head'], 'expected_dirty_digest': remote['dirty_digest'], 'files': []}
    syncing = mode == 'local' or sync == 'local-head'
    local = Path(platform['local_root'])
    local_head = None
    if mode in ('local', 'overlay'):
        local_head = command(['git', '-C', local, 'rev-parse', 'HEAD'])
        source['local_head'] = local_head
    if syncing:
        if mode not in ('local', 'overlay'):
            raise DeployError('同步本地 commit 需要 --source local 或 --source overlay')
        if remote['dirty']:
            raise DeployError('远端有 tracked 改动，不能快进同步；保留现场，需先处理改动或配置独立 checkout')
        result = subprocess.run(['git', '-C', str(local), 'merge-base', '--is-ancestor', remote['head'], local_head], capture_output=True)
        if result.returncode:
            raise DeployError('远端 HEAD 不是本地 HEAD 的已知祖先，不能快进；保留现场，需核实提交关系或配置独立 checkout')
        # Reject gitlink updates rather than silently building old submodule bytes.
        changed = subprocess.check_output(['git', '-C', str(local), 'diff', '--raw', '--no-renames', remote['head'], local_head, '--'])
        if any(any(part.lstrip(b':') == b'160000' for part in line.split(b'\t', 1)[0].split()[:2]) for line in changed.splitlines()):
            raise DeployError('同步范围涉及 Git 子模块，需单独处理完整子模块基线')
        source['sync'] = {'strategy': 'ff-only', 'base_head': remote['head'], 'target_head': local_head, 'bundle_sha256': None}
        source['sync']['commit_count'] = int(command(['git', '-C', local, 'rev-list', '--count', remote['head'] + '..' + local_head]))
        source['sync']['commits'] = command(['git', '-C', local, 'log', '--format=%H %s', '-30', remote['head'] + '..' + local_head]).splitlines()
    if mode in ('overlay', 'local'):
        if not file_list:
            if mode == 'overlay':
                raise DeployError('overlay 需要 --files-from 明确列出完整依赖文件')
            paths = []
        else:
            paths = read_file_list(file_list)
        if local_head != remote['head'] and not syncing:
            raise DeployError('本地和远端 HEAD 不同；可用 --sync local-head 做锁内快进同步，再覆盖编译')
        dirty_local = subprocess.check_output(['git', '-C', str(local), 'diff', '--name-only', '-z', 'HEAD', '--']).decode().split('\0')
        source['excluded_tracked_changes'] = sorted(set(x for x in dirty_local if x) - set(paths))
        if mode == 'local' and not paths and source['excluded_tracked_changes'] and not committed_only:
            raise DeployError('本地存在未提交 tracked 改动；用 --files-from 明确本次编译文件，不能把它们当成已同步')
        code = """import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); out={}
for name in json.loads(sys.argv[2]):
 p=root/name
 if not p.resolve().is_relative_to(root) or p.is_symlink() or any(a.is_symlink() for a in p.parents if a!=root): raise RuntimeError('unsafe path')
 out[name]=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
print(json.dumps(out))
"""
        baseline = ({name: committed_file_sha(local, local_head, name) for name in paths} if syncing else
                    json.loads(ssh(platform, ['python3', '-c', code, platform['remote_root'], json.dumps(paths)])))
        for name in paths:
            p = checked_file(local, name)
            source['files'].append({'path': name, 'sha256': digest(p), 'baseline_sha256': baseline[name]})
    elif file_list:
        raise DeployError('--files-from 只能搭配 --source overlay')
    if committed_only:
        source['committed_only'] = True
    return source


def freeze_bundle(path, platform, source):
    sync = source.get('sync')
    if not sync or sync['base_head'] == sync['target_head']:
        return
    bundle = path / 'source.bundle'
    git = ['git', '-C', platform['local_root'], '-c', 'core.hooksPath=/dev/null']
    temporary_ref = 'refs/kbe-deploy/' + uuid.uuid4().hex
    command(git + ['update-ref', temporary_ref, sync['target_head'], ''])
    try:
        command(git + ['bundle', 'create', bundle, temporary_ref, '^' + sync['base_head']], timeout=600)
    finally:
        # Never delete a ref if another writer has unexpectedly changed it.
        command(git + ['update-ref', '-d', temporary_ref, sync['target_head']])
    command(['git', '-C', platform['local_root'], 'bundle', 'verify', bundle], timeout=120)
    sync['bundle_sha256'] = digest(bundle)


def prepare(args, persist=False):
    platforms = config()['platforms']
    if args.platform not in platforms:
        raise DeployError('未知平台: ' + args.platform)
    platform = platforms[args.platform]
    if not platform.get('ready', True):
        raise DeployError(platform.get('not_ready_reason', '平台远端配置尚未完成'))
    modules = select_modules(platform, args.modules)
    if args.action == 'collect' and args.source != 'remote':
        raise DeployError('拉取已有产物只支持 --source remote')
    if args.action == 'collect' and args.sync:
        raise DeployError('collect 不允许同步源码；同步基线必须搭配 build')
    if not 1 <= args.jobs <= 64:
        raise DeployError('--jobs 范围为 1..64')
    source = source_plan(platform, args.source, args.files_from, args.sync, getattr(args, 'committed_only', False))
    ident = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    payload = {'schema': 1, 'id': ident, 'action': args.action, 'platform': {k: v for k, v in platform.items() if k != 'modules'},
               'modules': modules, 'source': source, 'jobs': args.jobs, 'created_at': now(),
               'deploy': {'serial': args.serial, 'allow_fingerprint_mismatch': args.allow_fingerprint_mismatch,
                          'accept_existing': args.accept_existing}}
    if args.serial:
        if args.action == 'collect' and not args.accept_existing:
            raise DeployError('已有产物无源码构建证明；部署需 --accept-existing')
        from device import Device
        Device(args.serial).check_platform(platform)
    if persist:
        p = STATE / 'jobs' / ident
        p.mkdir(parents=True)
        freeze_bundle(p, platform, source)
        for f in source['files']:
            dst = p / 'overlay' / f['path']
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked_file(platform['local_root'], f['path']), dst)
            if digest(dst) != f['sha256']:
                raise DeployError('准备过程中源码变化，请重新提交')
        save_json(p / 'job.json', payload)
        state(p, 'queued', id=ident)
    return payload


def start_worker(path, mode='run'):
    with (path / 'worker.log').open('a') as log:
        proc = subprocess.Popen([sys.executable, str(ROOT / 'kbe_deploy.py'), '_worker', path.name, '--mode', mode],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                env=dict(os.environ, PYTHONUNBUFFERED='1'))
    save_json(path / 'pid.json', {'pid': proc.pid, 'started_at': now()})


@contextlib.contextmanager
def job_lock(path):
    with (path / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('此任务正在运行；不能同时下载、部署或回滚')
        yield


def verify_bundle(path):
    job = read_json(path / 'job.json')
    if not (path / 'manifest.json').exists():
        state = read_json(path / 'state.json') if (path / 'state.json').exists() else {}
        remote = state.get('remote_state', {}).get('state')
        ident = job['id']
        if remote == 'failed':
            reason = '远端编译/收集失败，未生成可部署产物'
            action = './deploy.sh logs ' + ident + ' --tail 80'
        elif remote == 'succeeded':
            reason = '远端已完成，但本地产物尚未下载完整'
            action = './deploy.sh resume ' + ident
        else:
            reason = '产物尚未就绪（任务状态: ' + state.get('state', 'unknown') + '）'
            action = './deploy.sh status ' + ident
        raise DeployError('任务 ' + ident + ' 不能部署：' + reason + '；请运行 ' + action)
    manifest = read_json(path / 'manifest.json')
    if manifest.get('schema') != 1 or manifest.get('job_id') != job['id'] or manifest.get('platform') != job['platform']['id']:
        raise DeployError('产物清单与任务/平台不匹配')
    if manifest.get('provenance') not in ('built', 'collected-existing'):
        raise DeployError('缺少产物来源')
    if job.get('source') is not None:
        requested = manifest.get('source', {}).get('requested')
        observed = manifest.get('source', {}).get('observed_before', {})
        if requested != job['source'] or observed.get('head') != job['source']['expected_head'] or observed.get('dirty_digest') != job['source']['expected_dirty_digest']:
            raise DeployError('产物源码清单与任务快照不匹配')
        if job['source'].get('sync'):
            build_base = manifest.get('source', {}).get('observed_build_base', {})
            if build_base.get('head') != job['source']['sync']['target_head'] or build_base.get('dirty') != []:
                raise DeployError('产物未证明在要求的本地 commit 干净基线上编译')
        expected_provenance = 'built' if job['action'] == 'build' else 'collected-existing'
        if manifest['provenance'] != expected_provenance:
            raise DeployError('产物来源与任务类型不匹配')
    expected = {}
    for module in job['modules']:
        for a in module['artifacts']:
            expected[a['path']] = a
    artifacts = manifest['artifacts']
    if len({a['path'] for a in artifacts}) != len(artifacts) or set(expected) != {a['path'] for a in artifacts}:
        raise DeployError('产物清单不完整或包含额外文件')
    for a in artifacts:
        actual = checked_file(path / 'artifacts', a['path'])
        if actual.stat().st_size != a['size'] or digest(actual) != a['sha256']:
            raise DeployError('产物哈希/大小不符: ' + a['path'])
        if a.get('kind') != expected[a['path']]['kind'] or a.get('bits') != expected[a['path']].get('bits'):
            raise DeployError('产物类型/位宽不符')
        if a['kind'] == 'elf':
            with actual.open('rb') as f:
                header = f.read(20)
            bits = a.get('bits')
            machine = int.from_bytes(header[18:20], 'little')
            if header[:4] != b'\x7fELF' or header[4] != {32: 1, 64: 2}.get(bits) or machine != {32: 40, 64: 183}.get(bits):
                raise DeployError('ELF 架构不符: ' + a['path'])
    return job, manifest


def _identity_probe(product_out, artifacts):
    """Read immutable build metadata without starting a build or touching a device."""
    return r'''import hashlib,json,pathlib,stat,sys
product=pathlib.Path(sys.argv[1]).resolve()
artifacts=json.loads(sys.argv[2])
out=product.parents[2]
def regular(path):
 st=path.stat()
 if not stat.S_ISREG(st.st_mode) or path.is_symlink(): raise RuntimeError("identity evidence is not a regular file: "+str(path))
 return path.read_bytes()
fingerprint=regular(product/"build_fingerprint.txt").decode("utf-8").strip()
variables_bytes=regular(out/"soong/soong.variables")
variables=json.loads(variables_bytes)
sdk=variables.get("Platform_sdk_version"); device=variables.get("DeviceName")
primary=variables.get("DeviceAbi"); secondary=variables.get("DeviceSecondaryAbi",[])
if not isinstance(sdk,int) or not isinstance(device,str) or not device or not isinstance(primary,list) or not isinstance(secondary,list): raise RuntimeError("soong variables missing product identity")
abis=primary+secondary
if not abis or not all(isinstance(x,str) and x for x in abis): raise RuntimeError("soong variables missing ABI identity")
hashes={}
for name in artifacts:
 candidate=(product/pathlib.PurePosixPath(name)).resolve()
 if candidate.is_relative_to(product) and str(pathlib.PurePosixPath(name))==name: hashes[name]=hashlib.sha256(regular(candidate)).hexdigest()
 else: raise RuntimeError("unsafe artifact evidence path")
print(json.dumps({"fingerprint":fingerprint,"fingerprint_sha256":hashlib.sha256((product/"build_fingerprint.txt").read_bytes()).hexdigest(),"soong_variables_sha256":hashlib.sha256(variables_bytes).hexdigest(),"artifact_sha256":hashes,"identity":{"ro.build.version.sdk":str(sdk),"ro.product.system.device":device,"ro.product.cpu.abilist":",".join(abis)}}))'''


def recover_identity(path):
    """Attach evidence for an old manifest which predates complete identity capture.

    The manifest stays immutable. Evidence is accepted only when the remote
    fingerprint is on the same incremental baseline, the remote checkout and
    artifacts still match the frozen build, and its product matches the profile.
    """
    path = Path(path)
    job, manifest = verify_bundle(path)
    if (path / 'deployment.json').exists():
        raise DeployError('已有部署记录，不能为历史任务补充身份证据')
    product = manifest.get('product', {})
    fingerprint = product.get('ro.build.fingerprint')
    if not isinstance(fingerprint, str) or not fingerprint:
        raise DeployError('历史 manifest 缺少 build fingerprint，不能建立身份关联')
    platform = job['platform']
    build_base = manifest.get('source', {}).get('observed_build_base', {})
    remote = inspect_remote(platform)
    if remote.get('head') != build_base.get('head') or remote.get('dirty'):
        raise DeployError('远端源码不再等于该任务实际构建基线，拒绝补充身份')
    expected_hashes = {item['path']: item['sha256'] for item in manifest.get('artifacts', [])}
    probe = json.loads(ssh(platform, ['python3', '-c', _identity_probe(platform['product_out'], sorted(expected_hashes)),
                                      platform['product_out'], json.dumps(sorted(expected_hashes))], timeout=120))
    identity = probe.get('identity')
    from device import incremental_only
    allowed_identity = {'ro.build.version.sdk', 'ro.product.system.device', 'ro.product.cpu.abilist'}
    if not isinstance(identity, dict) or set(identity) != allowed_identity or not incremental_only(probe.get('fingerprint'), fingerprint):
        raise DeployError('远端 build_fingerprint 不属于历史 manifest 的同一增量基线，拒绝补充身份')
    if probe.get('artifact_sha256') != expected_hashes:
        raise DeployError('远端产物哈希不再等于历史任务，拒绝补充身份')
    expected_product = platform.get('product')
    if identity.get('ro.product.system.device') != expected_product:
        raise DeployError('远端 soong product 与任务平台不匹配，拒绝补充身份')
    allowed_sdk = platform.get('device_match', {}).get('properties', {}).get('ro.build.version.sdk', [])
    if identity.get('ro.build.version.sdk') not in allowed_sdk:
        raise DeployError('远端 soong SDK 与任务平台不匹配，拒绝补充身份')
    expected_abis = set(platform.get('device_match', {}).get('abis', []))
    actual_abis = set(identity.get('ro.product.cpu.abilist', '').split(','))
    if not expected_abis.issubset(actual_abis):
        raise DeployError('远端 soong ABI 与任务平台不匹配，拒绝补充身份')
    evidence = {'schema': 1, 'job_id': job['id'], 'manifest_sha256': digest(path / 'manifest.json'),
                'manifest_fingerprint': fingerprint, 'identity': identity,
                'evidence': {'kind': 'soong.variables+build_fingerprint', 'server': platform['server'],
                             'product_out': platform['product_out'],
                             'observed_fingerprint': probe.get('fingerprint'),
                             'build_fingerprint_sha256': probe.get('fingerprint_sha256'),
                             'soong_variables_sha256': probe.get('soong_variables_sha256')},
                'recorded_at': now()}
    if not all(isinstance(evidence['evidence'][key], str) and re.fullmatch(r'[a-f0-9]{64}', evidence['evidence'][key])
               for key in ('build_fingerprint_sha256', 'soong_variables_sha256')):
        raise DeployError('远端身份证据哈希无效')
    save_json(path / 'identity-evidence.json', evidence)
    return evidence


def effective_product_identity(path, manifest):
    """Merge verified sidecar evidence for legacy manifests without rewriting them."""
    product = manifest.get('product', {})
    required = {'ro.build.version.sdk', 'ro.build.fingerprint'}
    if required.issubset(product) and any(key.startswith('ro.product.') and key.endswith('.device') for key in product):
        return product
    evidence_path = Path(path) / 'identity-evidence.json'
    if not evidence_path.is_file():
        return product
    evidence = read_json(evidence_path)
    if (evidence.get('schema') != 1 or evidence.get('job_id') != manifest.get('job_id')
            or evidence.get('manifest_sha256') != digest(Path(path) / 'manifest.json')
            or evidence.get('manifest_fingerprint') != product.get('ro.build.fingerprint')):
        raise DeployError('历史身份侧车证据与 manifest 不匹配')
    identity = evidence.get('identity')
    allowed_identity = {'ro.build.version.sdk', 'ro.product.system.device', 'ro.product.cpu.abilist'}
    if not isinstance(identity, dict) or set(identity) != allowed_identity or not all(isinstance(identity.get(key), str) and identity[key]
                                                                                       for key in allowed_identity):
        raise DeployError('历史身份侧车证据不完整')
    if any(key in product and product[key] != identity[key] for key in identity):
        raise DeployError('历史身份侧车证据与 manifest 的既有字段冲突')
    return {**identity, **product}


def download(path):
    job = read_json(path / 'job.json')
    p = job['platform']
    remote = p['remote_jobs'] + '/' + job['id']
    remote_state = json.loads(ssh(p, ['cat', remote + '/state.json']))
    if remote_state['state'] != 'succeeded':
        raise DeployError('远端任务未成功完成，不能下载部署')
    scp(p, remote + '/manifest.json', path / 'manifest.json')
    manifest = read_json(path / 'manifest.json')
    expected = {a['path'] for m in job['modules'] for a in m['artifacts']}
    if {a['path'] for a in manifest['artifacts']} != expected:
        raise DeployError('远端产物清单与请求不符')
    for a in manifest['artifacts']:
        name = relpath(a['path'])
        dst = path / 'artifacts' / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.part')
        scp(p, remote + '/artifacts/' + name, tmp)
        if digest(tmp) != a['sha256']:
            raise DeployError('下载校验失败: ' + name)
        os.replace(tmp, dst)
    verify_bundle(path)
    scp(p, remote + '/build.log', path / 'build.log')
    fetch_sync_receipt(path, job)


def fetch_sync_receipt(path, job):
    if not job['source'].get('sync'):
        return
    try:
        p = job['platform']
        remote = p['remote_jobs'] + '/' + job['id'] + '/sync.json'
        scp(p, remote, path / 'sync.json')
        receipt = read_json(path / 'sync.json')
        current = read_json(path / 'state.json')
        current['sync'] = {k: receipt[k] for k in ('state', 'before_head', 'target_head') if k in receipt}
        save_json(path / 'state.json', current)
    except Exception:
        # Pre-sync refusals have no receipt. The authoritative failure stays in state/logs.
        pass


def notify_completion(job, phase):
    # Local desktop notification only; never wakes a model or sends external messages.
    if sys.platform == 'darwin' and shutil.which('osascript'):
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "KBE 编译与替换"\nend run'
        try:
            command(['osascript', '-e', script, job['id'] + ': ' + phase], timeout=10)
        except Exception:
            pass


def worker(path, mode):
    job = read_json(path / 'job.json')
    p = job['platform']
    remote = p['remote_jobs'] + '/' + job['id']
    with (path / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('此任务已有运行中的 worker')
        try:
            if mode == 'run':
                state(path, 'uploading')
                ssh(p, ['mkdir', '-p', remote])
                scp(p, ROOT / 'remote_worker.py', remote + '/remote_worker.py', upload=True)
                scp(p, path / 'job.json', remote + '/job.json', upload=True)
                if job['source'].get('sync', {}).get('bundle_sha256'):
                    if digest(path / 'source.bundle') != job['source']['sync']['bundle_sha256']:
                        raise DeployError('本地 Git bundle 已改变，拒绝上传')
                    scp(p, path / 'source.bundle', remote + '/source.bundle', upload=True)
                for f in job['source']['files']:
                    dest = remote + '/overlay/' + f['path']
                    ssh(p, ['mkdir', '-p', str(PurePosixPath(dest).parent)])
                    scp(p, path / 'overlay' / f['path'], dest, upload=True)
                script = 'nohup python3 ' + shlex.quote(remote + '/remote_worker.py') + ' run ' + shlex.quote(remote) + ' >' + shlex.quote(remote + '/launcher.log') + ' 2>&1 < /dev/null & echo $!'
                remote_pid = ssh(p, ['sh', '-c', script])
                save_json(path / 'remote.json', {'path': remote, 'pid': remote_pid})
            elif not (path / 'remote.json').exists():
                raise DeployError('任务尚未记录远端启动结果，请检查远端目录，不会自动重复提交')
            deadline = time.monotonic() + 24 * 3600
            errors = 0
            while time.monotonic() < deadline:
                try:
                    value = json.loads(ssh(p, ['cat', remote + '/state.json']))
                    errors = 0
                    save_json(path / 'remote-state.json', value)
                except Exception as e:
                    errors += 1
                    state(path, 'waiting_remote', detail=str(e)[-1000:])
                    if errors >= 40:
                        raise DeployError('远端连续不可达；远端可能仍在运行，可用 resume 重新接管')
                    time.sleep(15)
                    continue
                phase = value['state']
                state(path, phase, remote_state={k: value[k] for k in ('state', 'error') if k in value})
                if phase == 'succeeded':
                    break
                if phase in ('failed', 'recovery_required'):
                    fetch_sync_receipt(path, job)
                    try:
                        scp(p, remote + '/build.log', path / 'build.log')
                    except Exception:
                        pass
                    raise DeployError(value.get('error', '远端构建失败'))
                time.sleep(15)
            else:
                raise DeployError('等待超过24小时；远端未被终止，请查询后 resume')
            state(path, 'downloading')
            download(path)
            if job['deploy']['serial']:
                from device import deploy
                deploy(path, **job['deploy'])
                state(path, 'deployed', acceptance='设备部署检查通过；真实播放待验收')
            else:
                state(path, 'succeeded', acceptance='编译/收集产物完成，未部署')
        except Exception as e:
            record = read_json(path / 'deployment.json') if (path / 'deployment.json').exists() else {}
            state(path, 'recovery_required' if record.get('state') == 'recovery_required' else 'failed', error=str(e))
            print(str(e), file=sys.stderr)
        finally:
            notify_completion(job, read_json(path / 'state.json')['state'])


def list_devices():
    from device import Device
    lines = command(['adb', 'devices']).splitlines()[1:]
    out = []
    for line in lines:
        cols = line.split()
        if len(cols) >= 2 and cols[1] == 'device':
            props = Device(cols[0]).properties()
            out.append({'serial': cols[0], 'product': props.get('ro.product.device'),
                        'platform': props.get('ro.board.platform'), 'sdk': props.get('ro.build.version.sdk')})
    return out


def menu_fingerprint_override(path, serial):
    from device import Device, validate_identity, incremental_only
    job, manifest = verify_bundle(path)
    manifest = {**manifest, 'product': effective_product_identity(path, manifest)}
    props = Device(serial).check_platform(job['platform'])
    validate_identity(job['platform'], props, manifest, True)
    actual = props.get('ro.build.fingerprint')
    expected = manifest['product']['ro.build.fingerprint']
    if actual == expected or incremental_only(actual, expected):
        return False
    print('设备 fingerprint: ' + str(actual))
    print('产物 fingerprint: ' + expected)
    print('平台/产品/SDK/ABI 检查通过；这些检查不证明跨构建接口兼容。')
    if any(m['id'] in ('services', 'audio-framework') for m in job['modules']):
        print('本任务含框架组件：需确认设备系统基线与完整框架/ART 配套兼容。')
    if input('已核实兼容并继续本次部署，输入 allow-fingerprint-mismatch；回车取消: ').strip() != 'allow-fingerprint-mismatch':
        raise DeployError('已取消部署；设备未替换，已有产物保留')
    return True


def menu_local_changes(platform):
    root = Path(platform['local_root'])
    def names(args):
        return {p for p in subprocess.check_output(['git', '-C', str(root), *args]).decode().split('\0') if p}
    tracked = names(['diff', '--name-only', '--no-renames', '-z', 'HEAD', '--'])
    untracked = names(['ls-files', '--others', '--exclude-standard', '-z'])
    paths = sorted(tracked | untracked)
    if not paths:
        print('本地无未提交改动，将编译本地 HEAD。')
        return []
    problems = {}
    for i, name in enumerate(paths, 1):
        try:
            if name.strip() != name or name.startswith('#') or '\n' in name or '\r' in name:
                raise DeployError('当前清单格式不支持此文件名')
            if any(part in ('.git', 'out', '.repo') for part in PurePosixPath(name).parts):
                raise DeployError('构建输出/版本控制路径不能覆盖')
            checked_file(root, name)
        except DeployError as error:
            problems[name] = str(error)
        label = '未跟踪' if name in untracked else '已修改/暂存'
        print(f'  {i}) [{label}] {name}' + (' — 不支持覆盖: ' + problems[name] if name in problems else ''))
    print('使用当前工作区文件内容（包含暂存后继续修改的内容）。')
    print('  1) 全部包含  2) 选择部分  3) 仅编译已提交版本')
    choice = input('本次改动范围: ').strip()
    if choice == '3':
        print('本次排除以上全部未提交改动，仅同步并编译 HEAD。')
        return ['--committed-only']
    if choice == '1':
        selected = paths
    elif choice == '2':
        indices = [int(x) for x in input('包含的文件编号（空格分隔）: ').split()]
        if not indices or any(i < 1 or i > len(paths) for i in indices):
            raise DeployError('无效文件编号')
        selected = [paths[i - 1] for i in sorted(set(indices))]
    else:
        raise DeployError('无效改动范围')
    for name in selected:
        if name in problems:
            raise DeployError('不能纳入 ' + name + ': ' + problems[name] + '；删除/重命名请先提交，或选择其他文件')
    for name in paths:
        print(('纳入: ' if name in selected else '排除: ') + name)
    directory = STATE / 'file-lists'
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / (uuid.uuid4().hex + '.txt')
    file.write_text('\n'.join(selected) + '\n')
    read_file_list(file)
    print('已生成文件清单: ' + str(file))
    return ['--files-from', str(file)]


def menu():
    data = config()['platforms']
    print('KBE 编译与替换（Linux-201 编译 / Mac 下载与 ADB 替换）')
    print('  1) 编译并下载  2) 拉取已有产物  3) 编译并替换  4) 部署已有任务  5) 回滚已有任务')
    action = input('操作: ').strip()
    if action in ('4', '5'):
        ident = input('任务 ID: ').strip()
        selected = jobdir(ident)
        job = read_json(selected / 'job.json')
        # Fail before device selection; deployment repeats verification under its job lock.
        manifest = verify_bundle(selected)[1] if action == '4' else None
        print('任务平台: ' + job['platform']['id'] + '；组件: ' + ','.join(m['id'] for m in job['modules']))
        print(json.dumps(list_devices(), ensure_ascii=False, indent=2))
        serial = input('设备序列号: ').strip()
        argv = ['deploy' if action == '4' else 'rollback', ident, '--serial', serial]
        if action == '4' and manifest['provenance'] == 'collected-existing':
            if input('这是已有产物，未证明对应当前源码。输入 accept-existing 接受并部署: ').strip() != 'accept-existing':
                raise DeployError('未接受已有产物部署')
            argv.append('--accept-existing')
        if action == '4' and menu_fingerprint_override(selected, serial):
            argv.append('--allow-fingerprint-mismatch')
        return main(argv)
    if action not in ('1', '2', '3'):
        raise DeployError('无效操作')
    keys = list(data)
    for i, key in enumerate(keys, 1):
        print(f'  {i}) {key}  {data[key]["lunch"]}')
    index = int(input('选择平台: '))
    if not 1 <= index <= len(keys):
        raise DeployError('无效平台编号')
    key = keys[index - 1]
    modules = list(data[key]['modules'])
    for i, m in enumerate(modules, 1):
        print(f'  {i}) {m}: {data[key]["modules"][m]["description"]}')
    indices = [int(x) for x in input('组件编号（空格分隔）: ').split()]
    if not indices or any(not 1 <= i <= len(modules) for i in indices):
        raise DeployError('无效组件编号')
    chosen = ','.join(modules[i - 1] for i in indices)
    argv = ['run', '--platform', key, '--modules', chosen, '--action', 'collect' if action == '2' else 'build']
    if action != '2':
        print('  1) 同步本地 commit + 本次文件  2) 远端现有源码  3) 同 HEAD 临时覆盖（不更新 commit）')
        source = input('源码来源: ').strip()
        if source == '1':
            argv += ['--source', 'local']
            argv += menu_local_changes(data[key])
        elif source == '3':
            argv += ['--source', 'overlay', '--files-from', input('文件清单绝对路径: ').strip()]
        elif source == '2':
            argv += ['--source', 'remote']
        else:
            raise DeployError('无效源码选择')
    else:
        argv += ['--source', 'remote']
    if action == '3':
        print(json.dumps(list_devices(), ensure_ascii=False, indent=2))
        serial = input('设备序列号（任务完成后自动替换并重启）: ').strip()
        if not serial:
            raise DeployError('必须选择设备')
        argv += ['--serial', serial]
    argv.append('--wait')
    return main(argv)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd')
    sub.add_parser('menu', help='交互菜单')
    listing = sub.add_parser('list', help='平台及组件')
    listing.add_argument('--full', action='store_true')
    sub.add_parser('devices', help='只读列出 ADB 设备')
    recover = sub.add_parser('recover-identity', help='为缺少 SDK/product/ABI 的历史 manifest 记录远端构建身份证据')
    recover.add_argument('id')
    for name in ('plan', 'run'):
        s = sub.add_parser(name, help='只读计划' if name == 'plan' else '提交后台任务')
        s.add_argument('--platform', required=True)
        s.add_argument('--modules', required=True, help='组件 ID，逗号分隔')
        s.add_argument('--action', choices=['build', 'collect'], default='build')
        s.add_argument('--source', choices=['remote', 'overlay', 'local'], required=True,
                       help='local: 同步本地 HEAD，再覆盖明确文件；remote: 远端现状；overlay: 同HEAD临时覆盖')
        s.add_argument('--sync', choices=['local-head'], help='为 overlay 启用本地 HEAD 快进同步（local 已内含）')
        s.add_argument('--files-from')
        s.add_argument('--committed-only', action='store_true', help='明确仅编译本地 HEAD，排除所有未提交修改')
        s.add_argument('--jobs', type=int, default=8)
        s.add_argument('--serial', help='指定后完成即部署并重启；省略则只下载')
        s.add_argument('--allow-fingerprint-mismatch', action='store_true')
        s.add_argument('--accept-existing', action='store_true')
        if name == 'run':
            s.add_argument('--wait', action='store_true', help='显示进度直到完成；Ctrl-C 只退出显示')
    watch_parser = sub.add_parser('watch', help='显示已有任务进度和最终结果')
    watch_parser.add_argument('id')
    for name in ('status', 'logs', 'fetch', 'resume'):
        s = sub.add_parser(name)
        s.add_argument('id', nargs='?' if name == 'status' else None)
        if name == 'logs':
            s.add_argument('--tail', type=int, default=80)
            s.add_argument('--remote', action='store_true', help='读取远端正在运行任务的日志尾部')
    for name in ('deploy', 'rollback'):
        s = sub.add_parser(name)
        s.add_argument('id')
        s.add_argument('--serial', required=True)
        if name == 'deploy':
            s.add_argument('--allow-fingerprint-mismatch', action='store_true')
            s.add_argument('--accept-existing', action='store_true')
    s = sub.add_parser('_worker', help=argparse.SUPPRESS)
    s.add_argument('id')
    s.add_argument('--mode', choices=['run', 'resume'], default='run')
    return p


def status_details(path):
    """Present pipeline evidence without rewriting historical job records."""
    value = read_json(path / 'state.json')
    job = read_json(path / 'job.json')
    remote = value.get('remote_state', {}).get('state')
    if remote == 'succeeded':
        value['build_state'] = 'succeeded' if job.get('action') == 'build' else 'not_built_collected'
    # This exact guard runs before root/remount and before the deployment journal.
    # Do not relabel download failures or partial deployments as safe to retry.
    if (value.get('state') == 'failed' and remote == 'succeeded'
            and value.get('error') == '设备与产物 fingerprint 不同；核实系统基线兼容后可显式使用 --allow-fingerprint-mismatch'
            and not (path / 'deployment.json').exists()):
        value.update(recorded_state='failed', state='deploy_blocked',
                     deployment_state='blocked_before_replace',
                     hint='编译/收集已完成；fingerprint 不同，部署前受阻。核实兼容性后用 deploy 复用产物，无需重新编译。')
    return value


def worker_alive(path):
    if not (path / 'pid.json').exists():
        return False
    pid = read_json(path / 'pid.json')['pid']
    try:
        os.kill(pid, 0)
        return '_worker ' + path.name in command(['ps', '-p', str(pid), '-o', 'args='], check=False)
    except ProcessLookupError:
        return False


def watch(path):
    job = read_json(path / 'job.json')
    print('任务: ' + path.name + '；Ctrl-C 退出进度显示，后台任务继续。', flush=True)
    labels = {'queued': '已提交', 'uploading': '上传任务/源码', 'waiting_remote': '等待服务器连接',
              'building': 'Linux 编译中', 'syncing': '同步源码', 'downloading': '下载并校验产物',
              'backing_up': '准备设备文件', 'deploying': '安装 APK / 替换系统库', 'rebooting': '重启并验证设备',
              'succeeded': '远端完成，处理产物', 'collecting': '收集服务器已有产物'}
    started = time.monotonic()
    tick = 0
    previous = None
    try:
        while True:
            value = status_details(path)
            phase = value['state']
            alive = worker_alive(path)
            # Remote success is published before downloading: it is not local completion.
            complete = phase in TERMINAL and (phase != 'succeeded' or bool(value.get('acceptance')))
            if complete:
                print('\r' + ' ' * 100 + '\r', end='')
                if phase in ('succeeded', 'deployed', 'rolled_back'):
                    if phase == 'succeeded':
                        print('完成：' + ('已有产物下载并校验成功（未编译、未部署）' if job.get('action') == 'collect' else '编译、下载及校验成功（未部署）'))
                        print('产物目录: ' + str(path / 'artifacts'))
                    else:
                        print('完成：' + ('部署及重启检查通过；真实播放待验收' if phase == 'deployed' else '回滚完成'))
                    return 0
                print('部署受阻：' if phase == 'deploy_blocked' else '任务未完成：', value.get('error', phase))
                print('日志: ./deploy.sh logs ' + path.name + ' --tail 80')
                return 1
            if not alive:
                print('\n本地 worker 已退出，尚无完整结果；远端可能仍运行。')
                print('检查: ./deploy.sh status ' + path.name + '；日志: ./deploy.sh logs ' + path.name)
                return 1
            if (path / 'deployment.json').exists():
                phase = read_json(path / 'deployment.json').get('state', phase)
            label = labels.get(phase, phase)
            elapsed = int(time.monotonic() - started)
            if sys.stdout.isatty():
                spinner = "|/-\\"[tick % 4]
                print(f'\r{spinner} {label} · 已等待 {elapsed // 60:02}:{elapsed % 60:02}     ', end='', flush=True)
            elif phase != previous:
                print(label, flush=True)
            previous = phase
            tick += 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print('\n已退出显示，后台任务继续。查看: ./deploy.sh watch ' + path.name)
        return 130


def main(argv=None):
    args = parser().parse_args(argv)
    if args.cmd in (None, 'menu'):
        if not sys.stdin.isatty():
            raise DeployError('非交互环境请使用参数命令；参见 --help')
        return menu()
    if args.cmd == 'list':
        data = config()
        if not args.full:
            data = {name: {'product': p['product'], 'ready': p.get('ready', True),
                           'modules': {key: m['description'] for key, m in p['modules'].items()}}
                    for name, p in data['platforms'].items()}
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.cmd == 'devices':
        print(json.dumps(list_devices(), ensure_ascii=False, indent=2))
    elif args.cmd == 'recover-identity':
        path = jobdir(args.id)
        with job_lock(path):
            evidence = recover_identity(path)
        print(json.dumps({'job_id': path.name, 'identity_evidence': str(path / 'identity-evidence.json'),
                          'product': evidence['identity']}, ensure_ascii=False, indent=2))
    elif args.cmd in ('plan', 'run'):
        payload = prepare(args, persist=args.cmd == 'run')
        if args.cmd == 'run':
            path = jobdir(payload['id'])
            start_worker(path)
            if args.wait:
                return watch(path)
            print(json.dumps({'id': path.name, 'state': 'queued', 'directory': str(path), 'status_command': './deploy.sh status ' + path.name}, ensure_ascii=False))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.cmd == 'status':
        paths = [jobdir(args.id)] if args.id else [p for p in sorted((STATE / 'jobs').glob('*'), reverse=True)
                                                   if (p / 'job.json').is_file() and (p / 'state.json').is_file()][:20]
        for path in paths:
            value = status_details(path)
            if (path / 'pid.json').exists():
                pid = read_json(path / 'pid.json')['pid']
                try:
                    os.kill(pid, 0)
                    process = command(['ps', '-p', str(pid), '-o', 'args='], check=False)
                    value['worker_alive'] = '_worker ' + path.name in process
                except ProcessLookupError:
                    value['worker_alive'] = False
                if value.get('state') not in TERMINAL and not value['worker_alive']:
                    value['hint'] = '本地 worker 已退出；远端可能仍运行。使用 resume 接管。'
            print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        path = jobdir(args.id)
        if args.cmd == 'watch':
            return watch(path)
        if args.cmd == '_worker':
            worker(path, args.mode)
        elif args.cmd == 'resume':
            if read_json(path / 'state.json')['state'] in ('deployed', 'rolled_back', 'recovery_required') or (path / 'deployment.json').exists():
                raise DeployError('已有部署记录，不自动重放；请使用明确的 deploy/rollback 操作')
            start_worker(path, 'resume')
            print('已接管任务 ' + path.name)
        elif args.cmd == 'logs':
            if args.remote:
                job = read_json(path / 'job.json')
                remote = job['platform']['remote_jobs'] + '/' + job['id'] + '/build.log'
                print(ssh(job['platform'], ['tail', '-n', str(max(1, min(args.tail, 1000))), remote]))
                return 0
            for file in ('build.log', 'worker.log'):
                if (path / file).exists():
                    print('\n' + file + ':')
                    from collections import deque
                    with (path / file).open(errors='replace') as f:
                        print(''.join(deque(f, maxlen=max(1, min(args.tail, 1000)))))
        elif args.cmd == 'fetch':
            with job_lock(path):
                download(path)
            print('产物下载并校验完成: ' + str(path / 'artifacts'))
        else:
            from device import deploy, rollback
            with job_lock(path):
                try:
                    if args.cmd == 'deploy':
                        deploy(path, args.serial, args.allow_fingerprint_mismatch, args.accept_existing)
                        state(path, 'deployed', acceptance='部署检查通过；真实播放待验收')
                    else:
                        rollback(path, args.serial)
                        state(path, 'rolled_back')
                except Exception as e:
                    record = read_json(path / 'deployment.json') if (path / 'deployment.json').exists() else {}
                    if record.get('state') == 'recovery_required':
                        state(path, 'recovery_required', error=str(e))
                    elif not record and args.cmd == 'deploy':
                        state(path, 'failed', error=str(e), deployment_state='blocked_before_replace')
                    elif record.get('state') in ('failed_before_replace', 'rolled_back'):
                        state(path, record['state'] if record['state'] == 'rolled_back' else 'failed', error=str(e))
                    raise
            print(read_json(path / 'deployment.json')['state'])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (DeployError, ValueError, IndexError, KeyError, OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print('错误: ' + str(e), file=sys.stderr)
        sys.exit(1)
