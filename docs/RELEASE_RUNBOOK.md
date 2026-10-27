# Release runbook

Single entry for publishing the CLI, the Mac client and the iPhone client. When asked to release,
read only this page. Open another guide only when a phase failure names it.

## Decisions (owner, 2026-10-27)

- `scripts/release.py` drives every release. Do not hand-edit version files, copy export recipes
  from task directories, or write one-off build, install or verification scripts.
- Default scope: every component that `plan` recommends, meaning product commits since its
  current version and release notes ready. Ask the owner only when `plan` recommends nothing,
  or when it reports a hold or missing notes that you cannot resolve from the commits.
- Gates: a new test failure or new compiler/build warning blocks publication. An entry
  registered in `scripts/release_known_issues.json` (in `cli/` or `apple/`) passes and is
  listed in the summary. Register an entry only with its origin, reason, residual risk,
  follow-up and evidence. On macOS the CLI suite must pass completely. Linux test failures may
  match registered environment-specific entries.
- Per-release evidence (logs, receipts, checksums, screenshots) stays in the release workspace.
  Guides record only durable findings.
- iPhone delivery succeeds once the AppShelf upload is verified. If direct installation fails,
  report it together with the Safari installation steps. Diagnosing MDM or AppShelf is a
  separate task.

## Commands

Run from the Corral workspace root. Every command prints one compact JSON object.

```bash
python3 cli/scripts/release.py plan                    # versions, changes, notes, recommendation
python3 cli/scripts/release.py start                   # bump, commit, snapshot, start background run
python3 cli/scripts/release.py start --components cli,ios
python3 cli/scripts/release.py wait                    # blocks up to 9 minutes; repeat until terminal
python3 cli/scripts/release.py status                  # one-shot state without waiting
python3 cli/scripts/release.py resume                  # after fixing the failed phase's cause
python3 cli/scripts/release.py summary                 # links and limits for the final reply
python3 cli/scripts/release.py rehearse --components mac,ios   # non-publishing phases only
```

`--bump minor|major` overrides the patch step. A `Release: minor` or `Release: major` line
under `## Unreleased` has the same effect. `Release: hold <reason>` keeps the component out
of the default scope.

## Release notes

`plan` reads `## Unreleased` in `cli/CHANGELOG.md` and `apple/CHANGELOG.md`. Each must hold
`### English` and `### 简体中文` with user-visible changes. When `plan` reports
`notes: missing`, write the notes from the listed commit subjects, commit only the changelog,
and rerun `plan`. `start` turns the section into the version heading and uses it as the
GitHub, Homebrew and AppShelf release notes.

## Phases

`start` commits only the version files and changelogs, clones each repository at that commit
into `~/.config/corral/development/releases/<id>/`, and runs two lanes in the background
through `agent_workflow.py` resource locks:

| Lane | Phases |
|---|---|
| CLI | `cli.env` → `cli.gate` (full macOS suite) → `cli.build` (macOS universal2 plus four Linux wheels and sdist; warnings gate) → `cli.linux` (installed manylinux wheel and full suite on the Linux host; failures gate) → `cli.tag` → `cli.publish` (GitHub assets, checksums, Homebrew formula) → `cli.verify` → `cli.install` (local editable install and Homebrew upgrade/test) |
| Apple | `apple.export` (sanitized public tree; privacy preflight) → `apple.ios_build` / `apple.mac_analyze` / `apple.mac_prepare` (warnings gate) → `apple.ios_deliver` → `apple.mac_install` → `apple.mac_publish` (notarization, GitHub release, update feed, Homebrew Cask) |

A gate failure stops its own lane before anything is published. The other lane continues.
`rehearse` runs the same phases in a scratch workspace with versions bumped only inside the
clones. It skips tagging, pushing, uploading, publishing and installing.

## When a phase fails

`wait` and `status` report the failed phase, the reason, the log path and at most 20 relevant
lines. Read the full log only when those lines do not explain the failure. Fix the cause, then
run `resume`. Phases that already succeeded are not repeated.

- Privacy preflight: add the value to `replace_all` in the private recipe so every exported
  file is covered, or add a per-path rule there. If a `replacement_file` is stale, refresh the
  file and its `source_sha256`. The recipe format is in
  [PUBLIC_SOURCE_EXPORT_GUIDE](PUBLIC_SOURCE_EXPORT_GUIDE.md).
- New warning: fix it if it is in owned code. Otherwise register it with an assessment.
- New Linux failure: fix it. Register it only after showing that it also fails on the
  previous release tag on the same host, or after you have identified an environment cause.
- Network or notarization interruption: `resume`. Every publication step is idempotent.

## Private configuration

`~/.config/corral/release/` stays outside every repository:

- `config.json`: `linux_host` (an SSH alias) and `ios_device` (the `ios-deliver` device name).
- `apple-export/recipe.json` and its replacement files: the single maintained public-export
  recipe for the Apple repository.

## Final reply

Report what `summary` returns: the published version links, the local installation result,
the iPhone delivery outcome, matched known issues and remaining limits. Do not copy logs.
