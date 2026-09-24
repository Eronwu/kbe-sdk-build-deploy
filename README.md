# KBE build/deploy skill

This repository contains a Codex skill and its Python build/deploy tool. It runs Android SDK builds on a configured Linux host; the local machine submits jobs, collects artifacts and optionally deploys selected component bundles to an explicit ADB device. The tool also works from a terminal without Codex.

## Install

Clone this private repository into your Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone <PRIVATE_REPO_URL> "${CODEX_HOME:-$HOME/.codex}/skills/kbe-build-deploy"
cd "${CODEX_HOME:-$HOME/.codex}/skills/kbe-build-deploy"
```

Requires Python 3.9+, Git, `ssh`, `scp`, and SSH access to the configured Linux SDK checkout. Device deployment also requires `adb`; APK updates may require `aapt2` or `aapt`. No Python package installation is needed. Each teammate uses their own SSH credentials and local SDK paths. The repository contains no credentials or job history.

Generate the untracked local config from the sanitized example:

```bash
python3 scripts/configure.py \
  --server USER@BUILD_HOST \
  --rk3568-local /absolute/path/to/rk356x_qy_hifi_sdk \
  --rk3568-remote /absolute/linux/path/to/rk356x_qy_hifi_sdk \
  --rk3576-local /absolute/path/to/rk3576_qy_hifi_sdk \
  --rk3576-remote /absolute/linux/path/to/rk3576_qy_hifi_sdk \
  --remote-jobs /absolute/linux/path/to/user-state/kbe-deploy/jobs
```

Use `--force` only to replace an existing local config intentionally. For different servers or job roots per platform, edit the generated `scripts/kbe-deploy/config/platforms.json` directly. Confirm your local paths and the server checkout before `plan` or `run`. SSH must work without an interactive password prompt. Local job state defaults to `~/.local/state/kbe-deploy`; set `KBE_DEPLOY_STATE` to move it, and use the same value when querying or recovering jobs.

## Use

```bash
./scripts/kbe-deploy/deploy.sh list
./scripts/kbe-deploy/deploy.sh devices
./scripts/kbe-deploy/deploy.sh plan --platform rk3576 --modules engine --action build --source local
./scripts/kbe-deploy/deploy.sh run --platform rk3576 --modules engine --action build --source local
./scripts/kbe-deploy/deploy.sh status JOB_ID
./scripts/kbe-deploy/deploy.sh logs JOB_ID --tail 80
```

`plan` is read-only and checks remote source state. `run` submits a detached build; it does not wait for completion. Add `--serial SERIAL` only for an authorized device replacement. For uncommitted local files, pass `--files-from /absolute/file-list.txt`; its entries are SDK-relative paths, one per line. `--source remote` deliberately uses the remote tree and ignores local edits. `--action collect` fetches existing artifacts, which are not build proof. See `./scripts/kbe-deploy/deploy.sh --help` for remaining commands.

The component profiles describe RK3568 (`rk3568_s`, SDK 32) and RK3576 (`rk3576_u`, SDK 34), including full artifact bundles and device identity checks. Inspect and adapt the profile if a team's product or output paths differ. SDK compilation belongs on Linux; this package's local tests do not compile SDK code or touch devices.

## Offline validation

```bash
python3 -m unittest discover -s scripts/kbe-deploy/tests -v
bash -n scripts/kbe-deploy/deploy.sh
python3 -m py_compile scripts/configure.py scripts/kbe-deploy/kbe_deploy.py scripts/kbe-deploy/device.py scripts/kbe-deploy/remote_worker.py
```

Keep `scripts/kbe-deploy/config/platforms.json` and local state out of Git. Before sharing an updated package, check the staged file list and scan for local paths, device IDs, keys and job logs.
