---
name: corral-workflow
description: Author, preview, run, reuse, and iterate multi-stage Python workflows and playbooks across Claude Code, Codex, OpenCode, Cursor, Pi, and Antigravity via `corral workflow` (`corral wf`).
---

# corral workflow: multi-stage Python orchestration

Use `corral workflow` for explicit cross-runtime multi-stage pipelines, high-fan-out audits, or resumable checkpointed workflows. Do **not** start a workflow engine for ordinary single-step delegation, Q&A, ordinary code review, or plain parallel requests; always preview read-only (`--dry-run`) first and wait for explicit user authorization before executing workers.

## Reuse-first and iterate loop

```bash
corral describe workflow                                               # discover subcommands and contracts
corral workflow playbook list --format tsv                             # check existing project/global playbooks first
corral workflow list --format tsv                                      # inspect recent runs (use `workflow script <wf_id>` to export source)
corral workflow playbook show <name>                                   # read playbook source, args, and historical optimization signals
corral workflow catalog --compact                                      # discover local capabilities, rankings & circuit health
corral workflow run <script_or_playbook> --arg goal="audit" --dry-run  # strictly read-only syntax & target/health preview
corral workflow run <script_or_playbook> --arg goal="audit" --max-concurrency 20
corral workflow graph <wf_id> --format ascii
corral workflow resume <wf_id> [--from-step <step_key>] [--override-target <step>=<target>] [--use-saved-script | --allow-script-change]
corral workflow cancel <wf_id> [--wait 5] [--force]
corral workflow playbook save <name> --from-run <wf_id> --scope project  # persist a proven/optimized script for reuse
corral workflow playbook export-skill <name> --scope project --dry-run   # opt-in thin pointer SKILL.md for high-frequency playbooks only
corral workflow health [--reset <target_id> --version <ver>]
```

- Scripts normally address **capability abstractions** (`ui-design`, `code-review`, `report-writing`, `deep-impl`, `debugging`, `cheap-fanout`, `fast-router`) resolved from Local Scan ∩ Cloud Capability Registry + User Overrides + Shared Circuit Breaker.
- Worker steps submit structured JSON via pure CLI (`corral workflow finish --wf <id> --step <id> --json-file <path>`), which validates against the step's JSON Schema immediately (exit `2` returns exact field errors to fix in the same turn).
- Workflow child sessions are hidden from the main session list by default (`corral list --include-hidden` opts in). Do not recursively launch a new workflow from inside a workflow child session.
