# Public source export

Dependency-free maintainer exporter (`scripts/export_public_source.py`) that copies one committed
snapshot as sanitized public source without its private Git history. The coordinator can take the
generated tree as a fresh root history for the public repository; the private source repository
stays intact. Public README and license content preparation is handled separately.

## What it does and does not do

- Reads every file through `git ls-tree` / `git cat-file` from the selected commit. Untracked
  files, working-tree edits and the `.git` directory can never leak into the output.
- Applies a private recipe: exact-path exclusions plus deterministic per-path text replacements,
  line removal, or a complete replacement file.
- Runs a privacy preflight over the resulting tree before declaring success.
- Prints a bounded JSON receipt (original commit, per-file hashes, exclusions, transformation
  names, output path). The receipt never contains recipe values, only transformation names.
- Does not initialize Git, commit, push or publish. It only writes the output tree and the receipt.

## Usage

```bash
python3 scripts/export_public_source.py \
  --source /absolute/path/to/private-checkout \
  --commit <exact-sha-or-HEAD> \
  --output /absolute/path/to/new-public-tree \
  --recipe /private/path/export-recipe.json \
  --forbidden-list /private/path/checker.json \
  --receipt /private/path/export-receipt.json
```

- `--source` and `--output` must be absolute. `--commit` defaults to `HEAD` and is resolved to a
  full SHA recorded in the receipt, so a dirty workspace cannot change the result.
- `--output` must not exist; a pre-existing directory is refused rather than clobbered, and output
  inside the source checkout is rejected. On any failure after writes begin, the tool removes the
  output directory it created and nothing else.
- `--recipe` and `--forbidden-list` are optional private inputs; `--receipt` optionally writes the
  same JSON receipt that is always printed to stdout.
- Exit codes: `0` success, `2` invalid usage or recipe, `1` source-tree or privacy failure.
- Bounds: at most 20000 files, 64 MiB per file, 512 MiB total, 1000 forbidden strings.

## Recipe example (placeholders only)

```json
{
  "exclude": [
    "docs/private-planning.md",
    "fixtures/internal-device-log.json"
  ],
  "replace": {
    "scripts/install.sh": {
      "replacements": [
        {"old": "https://relay.example.net", "new": "https://relay.example.invalid"},
        {"old": "TEAM-TOKEN-abc123", "new": "YOUR-RELAY-TOKEN"}
      ]
    },
    "docs/relay-setup.md": {
      "remove_lines_containing": ["INTERNAL-HOST-xyz"]
    },
    "config/sample.json": {
      "replacement_file": "public-sample.json"
    }
  },
  "forbidden_strings": [
    "INTERNAL-HOST-xyz",
    "PRIVATE-PLAN-2026-10-09"
  ]
}
```

Rules:

- Paths are exact, repository-relative and POSIX-style. Unknown, absolute, `..` or `.git` paths
  are rejected, as is a replacement targeting an excluded path.
- `replacements` apply in listed order to UTF-8 text; a pattern that matches nothing is an error so
  stale recipes fail loudly instead of silently shipping secrets.
- `remove_lines_containing` drops whole lines holding any listed substring.
- `replacement_file` supplies the complete new file content from a path relative to the recipe
  file, and cannot combine with other edits for the same path.
- Every non-excluded path without a replacement is copied byte-identically, so untransformed
  public bytes equal `git show <commit>:<path>`.
- The recipe may carry private values locally, but they never appear in the receipt or stdout.
  Never invent credentials or bake maintainer data into the tool or the recipe example.

`--forbidden-list` accepts the same `forbidden_strings` as a JSON object (`{"forbidden_strings":
[...]}`), a bare JSON array, or plain-text lines. Use it when the checker input is maintained
separately from the recipe.

## UI source is copy-only

The following application paths may only be copied byte-identically, never transformed — a
recipe replacement targeting them is rejected before any mutation:

- SwiftUI views: anything under `Shared/` `UI` (also `apple/Shared/UI`), and any `*.swift`
  path containing `/UI/`,
- client shells: `*.swift` under `iOS/` or `macOS/`,
- shared resources: anything under `Shared/Resources`.

Excluding such a file is allowed; editing one is not. `Shared/Core` stays transformable so an
explicit recipe can redact non-UI details while views, shells and resources keep byte equality
with the commit: the audit flow replaces the Keychain adapter carrying the hardcoded team
access group with a PUBLIC SNAPSHOT ONLY adapter that reads the build-expanded Info.plist
shared access group (see the Apple `PUBLIC_BUILD_GUIDE`; private signing configuration and
installed clients are unchanged). Configuration, build scripts, developer docs and test
fixtures may likewise be redacted. There is no automatic private-IP global replacement:
network behavior must only change through explicit per-path recipe entries.

## Privacy preflight

Before success the tool scans the planned output bytes and fails (cleaning up its output) when it
finds:

1. Non-placeholder personal paths: `/Users/<segment>/...` where the segment is not a placeholder
   (`<name>`-style brackets, `example`, `shared`, `yourname`, `username`). Put the private-plan
   marker and any other must-not-ship strings into `forbidden_strings` (recipe) or
   `--forbidden-list` (checker input).
2. Known private recipe values: every `old` replacement text and every `remove_lines_containing`
   substring must be gone from the whole tree, not just from the edited file.
3. Every configured forbidden string.

Error messages and the receipt name the offending file and check kind, never the matched value.
Synthetic or public keys (example tokens, `YOUR-...` placeholders, public-key blocks) are not
treated as secrets: only the personal-path pattern above is built in, everything else comes from
the maintainer's explicit forbidden lists.

## Repository owner

The exporter itself hardcodes no repository owner: every GitHub path it emits comes from the
committed tree or the recipe. Post-migration defaults live outside this tool — `install.sh`,
`scripts/publish-release.sh`, `scripts/homebrew_formula.py`, `src/corral/updater.py` and the
package metadata now target `corral-dev/corral` (tap `x0c/homebrew-tap` and upstream
`x0c/sesskit` unchanged). Recipe authors should likewise write `corral-dev/corral` paths;
GitHub Transfer redirects keep old `x0c/corral` links working for existing installs.

## Keeping private material out of the public repository

- Store recipes, replacement files, checker inputs and receipts under a private directory such as
  `~/.config/corral/development/<task>/`, never inside the exported tree or the public repository.
- Review the receipt's `exclusions`, `transforms` and `file_count` before handing the tree to the
  coordinator; spot-check that no private filename survived as a path.
- The exporter writes no `.git` directory. Initializing the fresh public history from the exported
  tree is the coordinator's separate step.
