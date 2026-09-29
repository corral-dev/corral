# Web task butler experiments

Risk experiments for `docs/design/WEB_TASK_BUTLER_DESIGN.md` §9. These are not product code
and are not packaged. Generated results under `results/` are ignored by git.

- `e1_injection.py` — create hosted sessions per assistant, send a first instruction, inject a
  mid-turn instruction, and verify delivery/completion from history files. Uses real
  assistant accounts; each run creates a disposable git folder under `/tmp/butler-e1/`.
- `editor/` — CodeMirror 6 editor spike (strike instead of delete, agent text, done spans,
  anchors, project hints). `npm install && npm run build`, `python editor/export_projects.py`, then
  `python editor/e2_editor_test.py` drives it in Chrome via agent-browser + CDP. `editor/preview.html`
  is a visual design draft using the same editor mechanics.
- `x_eval/` — X judgement corpus. Run `python x_eval/run_eval.py codex-luna` or
  `python x_eval/run_eval.py claude-haiku`; the model is explicitly low-cost. Results are
  `x_eval/results/e3-*.json`.
- `trigger_replay.py` — synthetic timestamped drafting traces for debounce and single-X-round
  behavior. Run `python trigger_replay.py`; results are `results/e4-trigger-replay.json`.
