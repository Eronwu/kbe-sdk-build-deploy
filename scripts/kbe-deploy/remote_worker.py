#!/usr/bin/env python3
"""Guarded, Linux-only build worker run on the SDK build host.

The caller creates an immutable job directory and starts this process with
``nohup``.  This program never chooses targets or deployment destinations: it
only builds a declared SDK snapshot, stages declared artifacts, and restores a
temporary overlay before reporting success.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


SCHEMA = 1
SAFE_PART = re.compile(r"^[A-Za-z0-9_.+,:@=-]+$")
# Ninja accepts relative path targets (for example out/.../services.art).
# Keep the shell-safe character policy while rejecting traversal and absolute
# paths; SAFE_PART remains intentionally stricter for lunch and module names.
SAFE_BUILD_TARGET = re.compile(r"^[A-Za-z0-9_./+,:@=-]+$")


class WorkerError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_git(root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(root), *arguments], stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorkerError(f"cannot inspect git checkout {root}: {exc}") from exc


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise WorkerError(f"unsafe relative path: {value!r}")
    return path


def _no_symlink_components(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if os.path.islink(current):
            raise WorkerError(f"symlink paths are not allowed: {relative}")
    return root / relative


def _regular_or_absent(root: Path, relative: Path) -> Path:
    path = _no_symlink_components(root, relative)
    if path.exists() and not path.is_file():
        raise WorkerError(f"not a regular file: {relative}")
    return path


def _parse_status(data: bytes, root: Path) -> list[dict[str, Any]]:
    # porcelain -z uses "XY path\0" (and a second path for rename/copy).
    fields = data.decode("utf-8", "surrogateescape").split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            raise WorkerError("unexpected git status porcelain output")
        xy, raw_path = field[:2], field[3:]
        paths = [raw_path]
        if "R" in xy or "C" in xy:
            if index >= len(fields) or not fields[index]:
                raise WorkerError("truncated git rename status")
            paths.append(fields[index])
            index += 1
        for raw in paths:
            relative = _safe_relative(raw)
            path = _regular_or_absent(root, relative)
            entries.append({"path": raw, "status": xy, "sha256": _sha256(path) if path.exists() else None})
    return sorted(entries, key=lambda item: (item["path"], item["status"]))


def inspect_source(sdk_root: str | Path) -> dict[str, Any]:
    """Return a stable, tracked-only source snapshot for local preflight too."""
    root = Path(sdk_root).resolve()
    if not root.is_dir():
        raise WorkerError(f"SDK root does not exist: {root}")
    head = _run_git(root, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise WorkerError("git did not return a full HEAD hash")
    dirty = _parse_status(_run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no"), root)
    submodules = _run_git(root, "submodule", "status", "--recursive").decode("utf-8", "replace").splitlines()
    changed_submodules = [line for line in submodules if line[:1] in ("+", "-", "U")]
    if changed_submodules:
        raise WorkerError("modified or unavailable recursive submodule is unsupported")
    return {"head": head, "dirty": dirty, "dirty_digest": _json_digest(dirty)}


def _state(job_dir: Path, state: str, error: str | None = None, manifest: dict[str, Any] | None = None,
           job_id: str | None = None) -> None:
    if job_id is None:
        try:
            candidate = json.loads((job_dir / "job.json").read_text(encoding="utf-8")).get("id")
            job_id = candidate if isinstance(candidate, str) and candidate else None
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    value: dict[str, Any] = {"id": job_id or job_dir.name, "state": state, "updated_at": int(time.time())}
    if error:
        value["error"] = error
    if manifest:
        value["manifest"] = manifest
    _atomic_json(job_dir / "state.json", value)


def _load_job(job_dir: Path) -> dict[str, Any]:
    try:
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"invalid job.json: {exc}") from exc
    if not isinstance(job, dict) or job.get("action") not in ("build", "collect"):
        raise WorkerError("job action must be build or collect")
    for key in ("id", "platform", "modules", "source"):
        if key not in job:
            raise WorkerError(f"job is missing {key}")
    if not isinstance(job["platform"], dict) or not isinstance(job["modules"], list) or not isinstance(job["source"], dict):
        raise WorkerError("invalid job platform, modules, or source")
    return job


def _source_expected(job: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None]:
    source = job["source"]
    if source.get("mode") not in ("remote", "overlay", "local"):
        raise WorkerError("source.mode must be remote, overlay, or local")
    head, digest = source.get("expected_head"), source.get("expected_dirty_digest")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise WorkerError("source.expected_head must be a 40-character SHA")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkerError("source.expected_dirty_digest must be a SHA-256")
    files = source.get("files", [])
    if not isinstance(files, list):
        raise WorkerError("source.files must be a list")
    sync = source.get("sync")
    if source.get("mode") == "local" and sync is None:
        raise WorkerError("local source mode requires an ff-only sync")
    if source.get("mode") == "remote" and sync is not None:
        raise WorkerError("remote source mode cannot request a sync")
    if sync is None:
        return head, digest, files, None
    if not isinstance(sync, dict) or sync.get("strategy") != "ff-only":
        raise WorkerError("source.sync must use ff-only")
    base, target, bundle_sha256 = sync.get("base_head"), sync.get("target_head"), sync.get("bundle_sha256")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise WorkerError("source.sync.base_head must be a 40-character SHA")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40}", target):
        raise WorkerError("source.sync.target_head must be a 40-character SHA")
    if bundle_sha256 is not None and (not isinstance(bundle_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256)):
        raise WorkerError("source.sync.bundle_sha256 must be a SHA-256 or null")
    if target != base and bundle_sha256 is None:
        raise WorkerError("source.sync needs source.bundle when target differs from base")
    if target == base and bundle_sha256 is not None:
        raise WorkerError("source.sync must not provide source.bundle when target equals base")
    return head, digest, files, sync


def _check_snapshot(snapshot: dict[str, Any], expected_head: str, expected_digest: str) -> None:
    if snapshot["head"] != expected_head or snapshot["dirty_digest"] != expected_digest:
        raise WorkerError("SDK source changed since the job was created")


def _git_dir(root: Path) -> Path:
    value = _run_git(root, "rev-parse", "--git-dir").decode().strip()
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _git_operation_in_progress(root: Path) -> bool:
    git_dir = _git_dir(root)
    return any((git_dir / name).exists() for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer"))


def _changed_paths_and_gitlinks(root: Path, base: str, target: str) -> list[Path]:
    raw = _run_git(root, "diff-tree", "--no-commit-id", "-r", "--raw", "-z", base, target)
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    changed: list[Path] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        try:
            if "\t" in field:
                header, raw_path = field.split("\t", 1)
            else:
                header = field
                raw_path = fields[index]
                index += 1
            old_mode, new_mode = header.split()[0][1:], header.split()[1]
        except (ValueError, IndexError) as exc:
            raise WorkerError("unexpected git diff-tree output") from exc
        if old_mode == "160000" or new_mode == "160000":
            raise WorkerError("sync cannot change submodule/gitlink commits")
        changed.append(_safe_relative(raw_path))
    return changed


def _untracked_and_ignored(root: Path, changed: list[Path]) -> list[Path]:
    """List only untracked bytes that a particular tree update could replace.

    Large Android trees commonly have an ignored ``out/`` with millions of
    paths.  Supplying the diff's pathspecs plus ``--directory`` lets Git return
    a single conflicting directory without walking unrelated output trees.
    """
    if not changed:
        return []
    paths: list[Path] = []
    for arguments in (("ls-files", "--others", "--exclude-standard", "--directory", "-z", "--"),
                      ("ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--")):
        output = _run_git(root, *arguments, *(str(path) for path in changed)).decode("utf-8", "surrogateescape")
        paths.extend(_safe_relative(item) for item in output.split("\0") if item)
    return paths


def _path_conflicts(candidate: Path, changed: Path) -> bool:
    return candidate == changed or candidate in changed.parents or changed in candidate.parents


def _reject_untracked_sync_conflicts(root: Path, changed: list[Path]) -> None:
    conflicts = {str(candidate) for candidate in _untracked_and_ignored(root, changed)
                 if any(_path_conflicts(candidate, target) for target in changed)}
    # ``ls-files -- path/to/child`` cannot report an ordinary untracked file
    # at ``path/to``.  Such a file or symlink blocks a tree update just as much
    # as an untracked child, so inspect only the finite ancestor chain.
    for target in changed:
        for parent in target.parents:
            if parent == Path("."):
                break
            candidate = root / parent
            if not (candidate.is_file() or candidate.is_symlink()):
                continue
            try:
                _run_git(root, "ls-files", "--error-unmatch", "--", str(parent))
            except WorkerError:
                conflicts.add(str(parent))
    if conflicts:
        rendered = ", ".join(sorted(conflicts))
        raise WorkerError("sync would overwrite untracked or ignored paths: " + rendered)


def _sync_state(job_dir: Path, before: dict[str, Any], target: str, state: str, **extra: Any) -> None:
    value: dict[str, Any] = {"schema": SCHEMA, "before_head": before["head"], "target_head": target,
                             "state": state, "updated_at": int(time.time())}
    value.update(extra)
    _atomic_json(job_dir / "sync.json", value)


def _fetch_bundle_target(root: Path, bundle: Path, target: str) -> None:
    """Import only the frozen bundle target, falling back to its advertised ref."""
    heads = _run_git(root, "bundle", "list-heads", str(bundle)).decode("utf-8", "surrogateescape").splitlines()
    matching_refs: list[str] = []
    for line in heads:
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[0] == target and fields[1].startswith("refs/"):
            matching_refs.append(fields[1])
    if len(matching_refs) != 1:
        raise WorkerError("source.bundle must advertise exactly one ref for target_head")
    fetch_prefix = ("-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "fetch", "--no-auto-gc", "--no-recurse-submodules",
                    "--no-tags", "--no-write-fetch-head", str(bundle))
    try:
        _run_git(root, *fetch_prefix, target)
    except WorkerError:
        # Some Git/file-transport combinations refuse an object SHA even when
        # the object is advertised by the bundle.  The checked header ref is
        # equivalent and still leaves remotes, branches and FETCH_HEAD alone.
        _run_git(root, *fetch_prefix, matching_refs[0])


def _sync_baseline(job_dir: Path, root: Path, before: dict[str, Any], sync: dict[str, Any]) -> dict[str, Any]:
    """Fast-forward only from a per-job bundle; never changes a configured remote."""
    base, target, bundle_sha256 = sync["base_head"], sync["target_head"], sync["bundle_sha256"]
    _sync_state(job_dir, before, target, "syncing", strategy="ff-only")
    try:
        if before["dirty"]:
            raise WorkerError("sync requires a clean tracked SDK working tree")
        if before["head"] != base:
            raise WorkerError("sync base does not match the inspected SDK HEAD")
        if _git_operation_in_progress(root):
            raise WorkerError("sync refused while a Git operation is in progress")
        try:
            branch = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
        except WorkerError:
            branch = None
        if target != base:
            bundle = _regular_or_absent(job_dir, Path("source.bundle"))
            if not bundle.exists() or _sha256(bundle) != bundle_sha256:
                raise WorkerError("source.bundle is missing or has an unexpected SHA-256")
            _run_git(root, "bundle", "verify", str(bundle))
            _fetch_bundle_target(root, bundle, target)
        elif (job_dir / "source.bundle").exists():
            raise WorkerError("source.bundle must be absent when sync target equals base")
        changed = _changed_paths_and_gitlinks(root, base, target)
        if _run_git(root, "merge-base", "--is-ancestor", base, target) != b"":
            # git prints no successful output; _run_git raises on a non-ancestor.
            raise WorkerError("unexpected merge-base output")
        _reject_untracked_sync_conflicts(root, changed)
        if target != base:
            _run_git(root, "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false", "-c", "merge.autoStash=false",
                     "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", target)
        observed = inspect_source(root)
        if observed["head"] != target or observed["dirty"]:
            raise WorkerError("SDK is not clean at the requested synced baseline")
        _sync_state(job_dir, before, target, "synced", branch=branch, detached_head=branch is None,
                    observed_build_base=observed)
        return observed
    except Exception as exc:
        try:
            actual_head = _run_git(root, "rev-parse", "HEAD").decode().strip()
        except WorkerError:
            actual_head = None
        _sync_state(job_dir, before, target, "failed", error=str(exc), actual_head=actual_head)
        raise


def _validate_overlay(job_dir: Path, root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    staged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
            raise WorkerError("each source file needs path and sha256")
        relative = _safe_relative(item["path"])
        if item["path"] in seen or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise WorkerError("duplicate source path or invalid overlay SHA")
        seen.add(item["path"])
        overlay = _regular_or_absent(job_dir / "overlay", relative)
        target = _regular_or_absent(root, relative)
        if not overlay.exists() or _sha256(overlay) != item["sha256"]:
            raise WorkerError(f"overlay bytes missing or changed: {relative}")
        baseline = item.get("baseline_sha256")
        if baseline is not None and (not isinstance(baseline, str) or not re.fullmatch(r"[0-9a-f]{64}", baseline)):
            raise WorkerError(f"invalid baseline SHA: {relative}")
        if baseline is None:
            if target.exists():
                raise WorkerError(f"overlay target was expected absent: {relative}")
        elif not target.exists() or _sha256(target) != baseline:
            raise WorkerError(f"overlay baseline changed: {relative}")
        staged.append({"relative": relative, "overlay": overlay, "target": target, "baseline": baseline})
    overlay_root = job_dir / "overlay"
    if overlay_root.exists():
        if overlay_root.is_symlink() or not overlay_root.is_dir():
            raise WorkerError("overlay must be a real directory")
        actual: set[str] = set()
        for candidate in overlay_root.rglob("*"):
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file() and not candidate.is_dir()):
                raise WorkerError(f"overlay contains a non-regular path: {candidate}")
            if candidate.is_file():
                actual.add(str(candidate.relative_to(overlay_root)))
        if actual != seen:
            raise WorkerError("overlay contains files not explicitly declared by source.files")
    return staged


def _apply_overlay(job_dir: Path, root: Path, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    backups: list[dict[str, Any]] = []
    backup_root = job_dir / "backups"
    for change in changes:
        relative, target = change["relative"], change["target"]
        existed = target.exists()
        entry: dict[str, Any] = {"path": str(relative), "existed": existed, "sha256": _sha256(target) if existed else None}
        if existed:
            backup = _regular_or_absent(backup_root, relative)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, backup)
            entry["mode"] = target.stat().st_mode & 0o777
        backups.append(entry)
    _atomic_json(root / ".kbe-deploy.transaction.json", {"schema": SCHEMA, "job_dir": str(job_dir), "backups": backups})
    for change in changes:
        target, overlay = change["target"], change["overlay"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay, target)
    return backups


def _journal_backups(root: Path, job_dir: Path) -> list[dict[str, Any]]:
    journal = root / ".kbe-deploy.transaction.json"
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"overlay transaction is unreadable: {exc}") from exc
    if value.get("schema") != SCHEMA or value.get("job_dir") != str(job_dir) or not isinstance(value.get("backups"), list):
        raise WorkerError("overlay transaction does not belong to this job")
    return value["backups"]


def _restore_overlay(job_dir: Path, root: Path, backups: list[dict[str, Any]]) -> None:
    backup_root = job_dir / "backups"
    for entry in backups:
        relative = _safe_relative(entry["path"])
        target = _regular_or_absent(root, relative)
        if entry["existed"]:
            backup = _regular_or_absent(backup_root, relative)
            if not backup.exists() or _sha256(backup) != entry["sha256"]:
                raise WorkerError(f"backup is unavailable or corrupt: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup, target)
            os.chmod(target, entry["mode"])
        elif target.exists():
            target.unlink()
    for entry in backups:
        target = _regular_or_absent(root, _safe_relative(entry["path"]))
        if entry["existed"]:
            if not target.exists() or _sha256(target) != entry["sha256"] or (target.stat().st_mode & 0o777) != entry["mode"]:
                raise WorkerError(f"restoration verification failed: {entry['path']}")
        elif target.exists():
            raise WorkerError(f"restoration verification failed: {entry['path']}")
    transaction = root / ".kbe-deploy.transaction.json"
    if transaction.exists():
        transaction.unlink()


def _artifact_specs(job: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for module in job["modules"]:
        if not isinstance(module, dict) or not isinstance(module.get("artifacts"), list):
            raise WorkerError("each module needs artifacts")
        for artifact in module["artifacts"]:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or artifact.get("kind") not in ("elf", "apk", "jar", "art", "odex", "vdex"):
                raise WorkerError("invalid artifact specification")
            _safe_relative(artifact["path"])
            if artifact["path"] in seen:
                existing = next(item for item in result if item["path"] == artifact["path"])
                if {key: existing.get(key) for key in ("path", "kind", "bits")} != {key: artifact.get(key) for key in ("path", "kind", "bits")}:
                    raise WorkerError(f"conflicting duplicate artifact: {artifact['path']}")
                continue
            seen.add(artifact["path"])
            result.append(artifact)
    if not result:
        raise WorkerError("job declares no artifacts")
    return result


def _product_out(root: Path, job: dict[str, Any]) -> Path:
    value = job["platform"].get("product_out")
    if not isinstance(value, str) or not value:
        raise WorkerError("platform.product_out is required")
    candidate = Path(value)
    path = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkerError("product_out must be inside SDK root") from exc
    if not path.is_dir():
        raise WorkerError(f"product_out does not exist: {path}")
    return path


def _stage_artifacts(job_dir: Path, product_out: Path, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    staged: list[dict[str, Any]] = []
    for spec in specs:
        relative = _safe_relative(spec["path"])
        source = _regular_or_absent(product_out, relative)
        if not source.exists() or source.stat().st_size == 0:
            raise WorkerError(f"missing or empty artifact: {relative}")
        source_hash = _sha256(source)
        destination = _regular_or_absent(job_dir / "artifacts", relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied_hash = _sha256(destination)
        if copied_hash != source_hash or _sha256(source) != source_hash:
            raise WorkerError(f"artifact changed while being staged: {relative}")
        entry = {"path": spec["path"], "sha256": copied_hash, "size": destination.stat().st_size, "kind": spec["kind"]}
        if "bits" in spec:
            entry["bits"] = spec["bits"]
        staged.append(entry)
    return staged


def _product_props(product_out: Path) -> dict[str, str]:
    """Read the stable identity fields the local deployer validates.

    system wins when vendor carries an old duplicated property.  Keeping the
    properties flat makes a manifest directly consumable by strict callers.
    """
    wanted = {"ro.build.version.sdk", "ro.build.fingerprint"}
    result: dict[str, str] = {}
    for relative in (Path("system/build.prop"), Path("system_ext/build.prop"), Path("system_ext/etc/build.prop"),
                     Path("product/build.prop"), Path("product/etc/build.prop"), Path("vendor/build.prop")):
        path = _regular_or_absent(product_out, relative)
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in raw_line or raw_line.startswith("#"):
                continue
            key, value = raw_line.split("=", 1)
            if key in wanted or (key.startswith("ro.product.") and (key.endswith(".device") or key.endswith(".name"))):
                result.setdefault(key, value)
            if key == "ro.system.build.fingerprint":
                result.setdefault("ro.build.fingerprint", value)
    for relative in (Path("build_fingerprint.txt"), Path("system/etc/build_fingerprint.txt"), Path("system_ext/etc/build_fingerprint.txt")):
        path = _regular_or_absent(product_out, relative)
        if path.exists() and path.stat().st_size:
            result.setdefault("ro.build.fingerprint", path.read_text(encoding="utf-8", errors="replace").strip())
    # Incremental module builds need not materialise partition build.prop files.
    # soong.variables is produced by lunch and is the build's own product/SDK/ABI
    # declaration, so it is a safe fallback only for fields absent above.
    try:
        out_root = product_out.parents[2]
        soong = _regular_or_absent(out_root, Path("soong/soong.variables"))
        variables = json.loads(soong.read_text(encoding="utf-8")) if soong.exists() else {}
    except (IndexError, json.JSONDecodeError):
        variables = {}
    if isinstance(variables, dict):
        sdk = variables.get("Platform_sdk_version")
        name = variables.get("DeviceName")
        primary = variables.get("DeviceAbi")
        secondary = variables.get("DeviceSecondaryAbi", [])
        if isinstance(sdk, int):
            result.setdefault("ro.build.version.sdk", str(sdk))
        if isinstance(name, str) and name:
            result.setdefault("ro.product.system.device", name)
        abis = primary + secondary if isinstance(primary, list) and isinstance(secondary, list) else []
        if abis and all(isinstance(value, str) and value for value in abis):
            result.setdefault("ro.product.cpu.abilist", ",".join(abis))
    return dict(sorted(result.items()))


def _targets(job: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for module in job["modules"]:
        values = module.get("targets", [])
        if not isinstance(values, list):
            raise WorkerError("module targets must be a list")
        for value in values:
            if not isinstance(value, str) or not SAFE_BUILD_TARGET.fullmatch(value):
                raise WorkerError(f"unsafe build target: {value!r}")
            target_path = Path(value)
            if target_path.is_absolute() or ".." in target_path.parts:
                raise WorkerError(f"unsafe build target: {value!r}")
            targets.append(value)
    if not targets:
        raise WorkerError("build job declares no build targets")
    return targets


def _other_build_running(root: Path) -> bool:
    """Detect Soong/Ninja using this checkout without blocking other SDKs."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    sdk_root = root.resolve()
    out_root = sdk_root / "out"

    def below_sdk(value: str) -> bool:
        try:
            Path(value).resolve().relative_to(sdk_root)
            return True
        except (OSError, ValueError):
            return False

    def below_out(value: str) -> bool:
        try:
            Path(value).resolve().relative_to(out_root)
            return True
        except (OSError, ValueError):
            return False

    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or entry.name == str(os.getpid()):
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        command_name = command.lower()
        if not any(name in command_name for name in ("ninja", "soong", "soong_build")):
            continue
        if below_sdk(cwd):
            return True
        # Launchers can retain another cwd while passing absolute SDK output
        # paths to Ninja/Soong.
        if any(argument.startswith("/") and below_out(argument) for argument in command.split()):
            return True
    return False


def _run_build(root: Path, job: dict[str, Any], log_path: Path) -> None:
    lunch = job["platform"].get("lunch")
    if not isinstance(lunch, str) or not SAFE_PART.fullmatch(lunch):
        raise WorkerError("unsafe or absent platform.lunch")
    jobs = job.get("jobs", 8)
    if not isinstance(jobs, int) or not 1 <= jobs <= 128:
        raise WorkerError("jobs must be between 1 and 128")
    envsetup = root / "build" / "envsetup.sh"
    # Android's standard build/envsetup.sh is a symlink to make/envsetup.sh.
    # It is trusted build infrastructure when it resolves inside this SDK.
    if not envsetup.is_file() or not envsetup.resolve().is_relative_to(root.resolve()):
        raise WorkerError("SDK build/envsetup.sh is unavailable")
    command = "source build/envsetup.sh && lunch {} && m {} -j{}".format(
        shlex.quote(lunch), " ".join(shlex.quote(target) for target in _targets(job)), jobs
    )
    with log_path.open("ab") as log:
        process = subprocess.Popen(["bash", "-lc", command], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        _RUNNING_PROCESS[0] = process
        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, b""):
                log.write(line)
                log.flush()
            code = process.wait()
        finally:
            # A SIGTERM handler raises WorkerError to reach overlay restoration.
            # Reap its build process here so it cannot outlive that transaction.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            _RUNNING_PROCESS[0] = None
    if code:
        raise WorkerError(f"build failed with exit code {code}; see {log_path}")


_RUNNING_PROCESS: list[subprocess.Popen[bytes] | None] = [None]


def _signal_handler(signum: int, _frame: Any) -> None:
    process = _RUNNING_PROCESS[0]
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    raise WorkerError(f"interrupted by signal {signum}")


def run_job(job_dir_value: str | Path) -> dict[str, Any]:
    if sys.platform != "linux":
        raise WorkerError("remote_worker may run only on a Linux build host")
    job_dir = Path(job_dir_value).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "build.log").touch(exist_ok=True)
    job = _load_job(job_dir)
    job_id = job["id"]
    if not isinstance(job_id, str) or not job_id:
        raise WorkerError("job id is required")
    _state(job_dir, "queued", job_id=job_id)
    root_value = job["platform"].get("remote_root")
    if not isinstance(root_value, str):
        raise WorkerError("platform.remote_root is required")
    root = Path(root_value).resolve()
    expected_head, expected_digest, source_files, sync = _source_expected(job)
    lock_path = root / ".kbe-deploy.lock"
    transaction = root / ".kbe-deploy.transaction.json"
    recovery = root / ".kbe-deploy.recovery.json"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerError("another kbe-deploy job holds the SDK lock") from exc
        if transaction.exists() or recovery.exists():
            raise WorkerError(f"stale overlay recovery state exists: {transaction if transaction.exists() else recovery}")
        if _other_build_running(root):
            raise WorkerError("another Soong/Ninja process appears to use this SDK")
        before = inspect_source(root)
        _check_snapshot(before, expected_head, expected_digest)
        product_out = _product_out(root, job)
        specs = _artifact_specs(job)
        backups: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        success = False
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        try:
            if job["action"] == "collect" and (job["source"].get("mode") != "remote" or source_files or sync):
                raise WorkerError("collect jobs require remote source mode with no overlay files")
            if sync:
                _state(job_dir, "syncing", job_id=job_id)
                build_base = _sync_baseline(job_dir, root, before, sync)
            else:
                build_base = before
            if job["action"] == "build":
                if job["source"].get("mode") == "remote" and source_files:
                    raise WorkerError("remote source mode cannot include overlay files")
                if job["source"].get("mode") == "overlay" or source_files:
                    changes = _validate_overlay(job_dir, root, source_files)
                    backups = _apply_overlay(job_dir, root, changes)
                _state(job_dir, "building", job_id=job_id)
                _run_build(root, job, job_dir / "build.log")
                # During an overlay, compare all unmodified paths plus the exact staged bytes.
                if changes:
                    for change in changes:
                        if _sha256(change["target"]) != _sha256(change["overlay"]):
                            raise WorkerError(f"overlay source drifted during build: {change['relative']}")
                    after = inspect_source(root)
                    before_rest = [entry for entry in build_base["dirty"] if entry["path"] not in {str(c["relative"]) for c in changes}]
                    after_rest = [entry for entry in after["dirty"] if entry["path"] not in {str(c["relative"]) for c in changes}]
                    if after["head"] != build_base["head"] or _json_digest(before_rest) != _json_digest(after_rest):
                        raise WorkerError("SDK source drifted during build")
                else:
                    _check_snapshot(inspect_source(root), build_base["head"], build_base["dirty_digest"])
            else:
                _state(job_dir, "collecting", job_id=job_id)
                _check_snapshot(inspect_source(root), build_base["head"], build_base["dirty_digest"])
            artifacts = _stage_artifacts(job_dir, product_out, specs)
            if job["action"] == "collect":
                _check_snapshot(inspect_source(root), build_base["head"], build_base["dirty_digest"])
            if backups:
                _state(job_dir, "restoring", job_id=job_id)
                _restore_overlay(job_dir, root, backups)
                _check_snapshot(inspect_source(root), build_base["head"], build_base["dirty_digest"])
            module_ids = [module.get("id") for module in job["modules"]]
            if any(not isinstance(module_id, str) or not module_id for module_id in module_ids):
                raise WorkerError("each module requires an id")
            manifest = {"schema": SCHEMA, "job_id": job["id"], "platform": job["platform"].get("id"), "modules": module_ids,
                        "source": {"requested": job["source"], "observed_before": before,
                                   "observed_build_base": build_base}, "product": _product_props(product_out),
                        "provenance": "built" if job["action"] == "build" else "collected-existing", "artifacts": artifacts,
                        "created_at": int(time.time())}
            _atomic_json(job_dir / "manifest.json", manifest)
            _state(job_dir, "succeeded", manifest=manifest, job_id=job_id)
            success = True
            return manifest
        finally:
            try:
                if (backups or transaction.exists()) and not success:
                    try:
                        _state(job_dir, "restoring", job_id=job_id)
                        _restore_overlay(job_dir, root, backups or _journal_backups(root, job_dir))
                    except Exception as restore_error:
                        _atomic_json(recovery, {"schema": SCHEMA, "job_dir": str(job_dir), "error": str(restore_error), "created_at": int(time.time())})
                        _state(job_dir, "recovery_required", f"build failed and restoration also failed: {restore_error}", job_id=job_id)
                        raise WorkerError(f"build failed and restoration also failed: {restore_error}") from restore_error
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("sdk_root")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("job_dir")
    args = parser.parse_args(argv)
    try:
        result = inspect_source(args.sdk_root) if args.command == "inspect" else run_job(args.job_dir)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        if args.command == "run":
            try:
                failed_job_dir = Path(args.job_dir).resolve()
                failed_job_dir.mkdir(parents=True, exist_ok=True)
                with (failed_job_dir / "build.log").open("a", encoding="utf-8") as log:
                    log.write(f"[remote_worker] FAILED: {exc}\n")
                existing = json.loads((failed_job_dir / "state.json").read_text(encoding="utf-8")) if (failed_job_dir / "state.json").exists() else {}
                if existing.get("state") != "recovery_required":
                    _state(failed_job_dir, "failed", str(exc))
            except Exception:
                pass
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
