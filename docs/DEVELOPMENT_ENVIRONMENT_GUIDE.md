# Development environment

## Requirements

Corral and SessKit development commands must use an explicitly selected checkout and that checkout's own `.venv`. Preparation may download and install only the dependencies declared for development; it must not alter system Python, an active `pipx` Corral installation, remote-daemon environments, or private user configuration. Keep app-install verification environments separate from developer environments.

The shared readiness entry must provide read-only `doctor`, idempotent `prepare`, a concise dependency-only `check`, and `run` for commands executed with the selected checkout's source on the import path. Every command identifies the target repository explicitly. Machine output uses the shared `{ok, data, error, meta}` envelope; ordinary output is brief, while detailed command logs persist under the target's ignored `.venv` directory. `doctor` must not download, install, or otherwise repair anything.

The check before a long test run reports interpreter identity/version, required tool availability and versions, source/package identity, dependency readiness, and lock/artifact mismatches. Missing or incompatible tools must be diagnosed before invoking the test suite. Preparation must converge on repeated runs and retain one interpretable environment per checkout.

Readiness blocks on lock or dependency-resolution drift: a missing or stale `uv.lock` is a failed check with a named blocker, never a warning alongside `ready:true`. Both checkouts verify with native uv resolution only: `uv lock --check` must succeed, and preparation syncs with `--locked`. No homemade version/specifier/marker resolver may stand in for the real resolver — a stale application lock must never be called current merely because a few installed versions fit declared ranges.

Corral's manifest keeps the published index range (`sesskit>=…`) so non-uv installers and offline packaging stay valid; the SessKit wheel that uv resolves comes from `[tool.uv.sources]` pointing at the immutable published artifact URL. `scripts/sesskit_dep.py` remains the canonical authority for that pin (version, URL, digest): the uv source entry is derived from it, and a focused test asserts the two stay identical. Development-only tools live in `[dependency-groups]` (`dev`: Ruff); they are never added to the published runtime dependencies. The build backend (maturin) stays in `[build-system]`; screenshot rendering keeps its existing optional cairosvg-or-fallback path and gains no new locked dependency in this wave.

## Commands

Run this entry from the Corral CLI checkout; always name the target checkout:

```bash
python3 scripts/dev_env.py doctor --repo . --json
python3 scripts/dev_env.py prepare --repo . --dry-run --json
python3 scripts/dev_env.py prepare --repo .
python3 scripts/dev_env.py check --repo .
python3 scripts/dev_env.py run --repo . -- env -u TEXTUAL_DISABLE_KITTY_KEY python scripts/ci-test.py

python3 scripts/dev_env.py doctor --repo ../../SessKit --json
python3 scripts/dev_env.py prepare --repo ../../SessKit
python3 scripts/dev_env.py check --repo ../../SessKit
python3 scripts/dev_env.py run --repo ../../SessKit -- python -m pytest -q
```

(From the Corral `cli/` checkout, the SessKit checkout sits at `../../SessKit`; always pass the explicit checkout path that is correct on the current machine, never a copied relative path.)

`doctor` is read-only (including `--json`) and reports the selected interpreter, local-source import path, exact tool versions, package state, `uv.lock` state, and blockers. `prepare` owns only the target checkout's `.venv`; its detailed command output is retained under `.venv/dev-env-logs/`. `prepare --dry-run` and `run --dry-run` plan without mutating anything: prepare reports whether the venv would be created, which sync/install steps would run, and why; run prints the resolved interpreter, working directory, and command without executing it. `check` is the short gate to run before expensive test work. `run` adds the target `src/` first on `PYTHONPATH`, disables user site packages, uses the target venv's interpreter/tools, and stores complete output in its log while returning a bounded tail. Exit status is authoritative; JSON callers always receive `{ok,data,error,meta}` on success, failure, and dry-run.

Corral test dependencies are synchronized from its lock with `--locked` (dev group plus `remote` extra, project itself not installed); the pinned SessKit release wheel and Ruff converge through the same sync plus an idempotent pinned install that records the install receipt. A stale or missing Corral lock blocks readiness with a named blocker; the check never repairs or rewrites it — `uv lock` regenerates it from the declared metadata (SessKit via the derived uv source, dev tools via the dev group). The serialized integration change must reconcile the application lock with the dependency handoff before treating the environment as release-ready.

## Lock and distribution boundary

SessKit is a library: its published runtime metadata declares supported dependency ranges and must not constrain downstream applications to a developer-only lock. Its checked-in `uv.lock` is development/repository metadata, including the pinned test and lint toolchain; it is not a runtime dependency or a library install contract.

Corral is an application and its environment must use an immutable dependency resolution. Its current `uv.lock` is the application/development resolution, but the environment command must report a stale lock rather than silently rewriting it. SessKit is not yet available from the package index used by clean installs: Corral's existing `scripts/sesskit_dep.py` is the authority for the exact published SessKit version, URL, and digest. Do not substitute a floating VCS install, a local editable SessKit checkout, or an arbitrary machine-installed package when validating Corral's published dependency pin.

## SessKit release handoff

Before a Corral release depending on SessKit, verify all of these separately against the same published SessKit pin:

1. Corral's isolated developer environment and full developer tests use the exact pinned SessKit release artifact.
2. Corral's clean-install check installs that same immutable artifact without relying on a developer checkout.
3. The actual installed Corral interpreter and the separately running remote-daemon interpreter each report the same SessKit version and artifact source. A successful Corral version display alone does not prove either dependency copy was updated.

Keep per-interpreter evidence (resolved executable, package source/version, and command result) in the verification log. A mismatch is a readiness failure, not a reason to skip tests or assume the active app changed.

Digest evidence for the pinned SessKit wheel: the installer checks the `#sha256` fragment at install time, but installed-package metadata (PEP 610 `direct_url.json`) does not reliably retain that digest. `prepare` therefore persists an install receipt (`.venv/dev-env-logs/sesskit-install-receipt.json`) recording the exact pin, the installed version, the recorded source URL, and the installer result. `doctor` reports the artifact as digest-verified only when a receipt matches the current pin and the installed distribution; a version-plus-URL match without a receipt is reported as exactly that, never as digest-verified.

## CI and release integration plan

The first implementation exposes environment preparation and test execution but does not replace the current CI/release entry points. In a serialized integration change:

1. Have `ci-test.py` run the complete suite under the selected checkout's ready `.venv` interpreter (re-exec when a checkout venv exists; the no-venv CI path is unchanged); keep module parallelism, full coverage, and retry/timeout behavior unchanged. A checkout venv that exists but is not ready fails fast with prepare guidance instead of running the suite against the wrong dependencies.
2. Keep pre-push's quick lint-only path, but prefer the prepared environment's pinned Ruff when it is ready. Keep release's complete-suite decision tied to the existing `ci_stamp.py` rather than introducing a second stamp.
3. Extend the existing stamp inputs to cover `uv.lock` and bind the stamp to the verified environment identity (interpreter version plus installed distribution set) in the same stamp file, so it is reusable only for the same source, interpreter and dependency resolution. A stale or missing environment never reuses a full-pass stamp. The stamp remains an optimization, never a way to skip required clean-install verification.
4. Keep clean-install verification in its own temporary environment. Before release, verify the pinned SessKit version/source in developer tests, clean install, installed Corral interpreter and remote-daemon interpreter independently.

Preserve UI/terminal acceptance and the no-partial-publish policy; environment setup is not permission to reduce verification coverage.

Environment identity probing must be hermetic: ambient `PYTHONPATH` (e.g. `ci-test.py` prepending `src/`) must never leak stale in-tree metadata such as `src/pickup.egg-info` into the installed-distribution listing. Probe the checkout venv with `PYTHONPATH`/`VIRTUAL_ENV` cleared and user site packages disabled.

## References

- uv project synchronization and lock behavior: <https://docs.astral.sh/uv/concepts/projects/sync/>
- uv dependency groups: <https://docs.astral.sh/uv/concepts/projects/dependencies/#development-dependencies>
