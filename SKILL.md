---
name: kbe-build-deploy
description: Build KBE RK3568 or RK3576 Android SDK components on a configured Linux server, collect artifacts, and optionally deploy or roll back a complete bundle on an explicitly selected ADB device. Use for KBE SDK module build, build-job status, artifact collection, and component replacement.
---

# KBE build and deployment

The bundled tool is `scripts/kbe-deploy/deploy.sh` relative to this skill directory. Resolve the installed `SKILL.md` directory before invoking it. Do not use another user's absolute path. If `scripts/kbe-deploy/config/platforms.json` is absent, follow this repository's `README.md` setup steps before any build or device action. The local host needs Python 3.9+, Git, SSH/SCP and, for device work, ADB. Compile the SDK only on the configured Linux build host.

## Plan and submit

1. Run `deploy.sh list` to see configured platforms and modules. Inspect the selected profile in `config/platforms.json` when paths or artifacts matter. Use `deploy.sh devices` and an explicit serial for device work.
2. Choose source intent: `--source local` freezes local HEAD, transfers unpublished commits and fast-forwards a clean remote ancestor; uncommitted changes require `--files-from` or an explicit `--committed-only`. `--source remote` uses the current remote checkout without a pull. `--source overlay --files-from` applies listed files temporarily and normally requires matching HEAD; `--sync local-head` enables baseline sync. The file list contains SDK-relative paths, one per line, and must include every uncommitted dependency. Never reset or clean a shared checkout to bypass a source conflict.
3. Run `plan` with the exact intended `run` arguments. Check source, platform, modules and device intent, then run. `run` starts a detached Linux build and returns a job ID. Add `--serial SERIAL` only when device replacement is within the user's request.

```bash
./scripts/kbe-deploy/deploy.sh list
./scripts/kbe-deploy/deploy.sh plan --platform rk3576 --modules engine --action build --source local
./scripts/kbe-deploy/deploy.sh run --platform rk3576 --modules engine --action build --source local
./scripts/kbe-deploy/deploy.sh status JOB_ID
./scripts/kbe-deploy/deploy.sh logs JOB_ID --tail 80
```

The local worker downloads and checks artifacts after the remote build. Submission is not completion. For a status request, inspect the job once; on failure, read the relevant log and structured state. `collect` labels existing output `collected-existing` and does not prove a build. Do not repeatedly submit a new job or replay an uncertain deployment. `resume` reattaches an interrupted local wait/download only when its recorded state permits it.

## Deployment boundaries

- Decoder and Engine APK dependencies travel as the configured bundle. Services requires `services.jar` with the matching ART/ODEX/VDEX files. Review selected artifacts before device replacement.
- A fingerprint mismatch beyond the incremental build number needs concrete compatibility evidence before a one-job `--allow-fingerprint-mismatch`. It never bypasses product, SDK, ABI or platform checks.
- A partial deployment or uncertain APK installation marked `recovery_required` needs journal and device inspection before further mutation. Preserve job records and backups.
- Report Linux build, artifact collection, deployment checks and real device playback acceptance separately. A verified file replacement is not functional acceptance.
- Module builds do not deliver property, sepolicy or other image configuration changes. Use the project's separate full-image workflow for those changes.

For full CLI behavior, failure handling and configuration fields, read `README.md` in this skill directory. Preserve the user's existing source and device changes.
