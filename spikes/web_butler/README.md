# Web task butler experiments

Risk experiments for `docs/design/WEB_TASK_BUTLER_DESIGN.md` §9. These are not product code
and are not packaged. Generated results under `results/` are ignored by git.

- `e1_injection.py` — create hosted sessions per assistant, send a first instruction, inject a
  mid-turn instruction, and verify delivery/completion from history files. Uses real
  assistant accounts; each run creates a disposable git folder under `/tmp/butler-e1/`.
- `editor/` — CodeMirror 6 editor spike: text the coordinator has seen is struck instead of
  deleted, unseen text deletes normally, plus coordinator text, done spans, anchors and project
  hints. `npm install && npm run build`, `python editor/export_projects.py`, then
  `python editor/e2_editor_test.py` drives it in Chrome via agent-browser + CDP.
  `editor/preview.html` (+ `src/demo.js`) is the backend-free interactive demo with a
  rule-based stand-in for the coordinator.
- `coordinator_eval/` — role prompts (`coordinator_prompt.md`, `worker_prompt.md`), the
  command boundary (`contract.py`), the judgement corpus (`scenarios.py`, run
  `python coordinator_eval/run_eval.py claude-haiku` or `codex-luna`; the model is explicitly
  low-cost) and the long-running single-session test (`long_run.py [--restart-at N]`).
  Results are `coordinator_eval/results/e3-*.json` and `e6-*.json`.
- `trigger_replay.py` — synthetic timestamped drafting traces for debounce and single-coordinator-round
  behavior. Run `python trigger_replay.py`; results are `results/e4-trigger-replay.json`.
