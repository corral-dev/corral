# Web task butler experiments

Throwaway risk experiments for `docs/design/WEB_TASK_BUTLER_DESIGN.md` §9. Not product code,
not packaged. Results append to `results/*.jsonl` (ignored by git).

- `e1_injection.py` — create hosted sessions per assistant, send a first instruction, inject a
  mid-turn instruction, and verify delivery/completion from history files. Uses real
  assistant accounts; each run creates a disposable git folder under `/tmp/butler-e1/`.
