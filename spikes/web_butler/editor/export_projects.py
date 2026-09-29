"""Write projects.json (local project + product names) for the editor spike. Not committed."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from corral import projects  # noqa: E402

names = {p.label for p in projects.discover()}
names |= {n.split("/")[0] for n in names}
Path(__file__).with_name("projects.json").write_text(json.dumps(sorted(names), ensure_ascii=False))
print(len(names), "names")
