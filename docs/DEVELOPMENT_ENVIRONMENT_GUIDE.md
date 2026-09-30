# Development environment

## Requirements

Corral and SessKit development commands must use an explicitly selected checkout and that checkout's own `.venv`. Preparation may download and install only the dependencies declared for development; it must not alter system Python, an active `pipx` Corral installation, remote-daemon environments, or private user configuration. Keep app-install verification environments separate from developer environments.

The shared readiness entry must provide read-only `doctor`, idempotent `prepare`, a concise dependency-only `check`, and `run` for commands executed with the selected checkout's source on the import path. Every command identifies the target repository explicitly. Machine output uses the shared `{ok, data, error, meta}` envelope; ordinary output is brief, while detailed command logs persist under the target's ignored `.venv` directory. `doctor` must not download, install, or otherwise repair anything.

The check before a long test run reports interpreter identity/version, required tool availability and versions, source/package identity, dependency readiness, and lock/artifact mismatches. Missing or incompatible tools must be diagnosed before invoking the test suite. Preparation must converge on repeated runs and retain one interpretable environment per checkout.

## Lock and distribution boundary

SessKit is a library: its published runtime metadata declares supported dependency ranges and must not constrain downstream applications to a developer-only lock. Its checked-in `uv.lock` is development/repository metadata, including the pinned test and lint toolchain; it is not a runtime dependency or a library install contract.

Corral is an application and its environment must use an immutable dependency resolution. Its current `uv.lock` is the application/development resolution, but the environment command must report a stale lock rather than silently rewriting it. SessKit is not yet available from the package index used by clean installs: Corral's existing `scripts/sesskit_dep.py` is the authority for the exact published SessKit version, URL, and digest. Do not substitute a floating VCS install, a local editable SessKit checkout, or an arbitrary machine-installed package when validating Corral's published dependency pin.

## SessKit release handoff

Before a Corral release depending on SessKit, verify all of these separately against the same published SessKit pin:

1. Corral's isolated developer environment and full developer tests use the exact pinned SessKit release artifact.
2. Corral's clean-install check installs that same immutable artifact without relying on a developer checkout.
3. The actual installed Corral interpreter and the separately running remote-daemon interpreter each report the same SessKit version and artifact source. A successful Corral version display alone does not prove either dependency copy was updated.

Keep per-interpreter evidence (resolved executable, package source/version, and command result) in the verification log. A mismatch is a readiness failure, not a reason to skip tests or assume the active app changed.

## CI and release integration plan

The first implementation exposes environment preparation and test execution but does not replace the current CI/release entry points. In a serialized integration change, route the existing complete check through the selected repository environment, preserve the full lint-plus-test contract and current successful-check stamp, and keep the standalone light pre-push lint path. The release workflow must use the same environment for the complete suite, keep clean-install verification isolated from `.venv`, and explicitly validate the installed app and remote-daemon dependency copies. Preserve parallel test lanes, UI/terminal acceptance requirements, and the no-partial-publish policy; environment setup is not permission to reduce verification coverage.

## References

- uv project synchronization and lock behavior: <https://docs.astral.sh/uv/concepts/projects/sync/>
- uv dependency groups: <https://docs.astral.sh/uv/concepts/projects/dependencies/#development-dependencies>
