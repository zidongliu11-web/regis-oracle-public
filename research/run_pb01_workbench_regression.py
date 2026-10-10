"""Two related synthetic regression checks with an isolated, pinned Arrow wheel."""
import importlib.metadata
import json
import platform
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ROOT / ".codex-local/pb01-test-deps"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEPENDENCIES))
import pyarrow
import pytest

expected = re.search(r"(?m)^pyarrow==([^\r\n]+)", (ROOT / "requirements.lock").read_text()).group(1)
if pyarrow.__version__ != expected or DEPENDENCIES.resolve() not in Path(pyarrow.__file__).resolve().parents:
    raise RuntimeError("isolated_arrow_dependency_not_bound")
report = json.loads((ROOT / ".codex-local/pb01-pip-report.json").read_text())
installation = next(i for i in report["install"] if i["metadata"]["name"].lower() == "pyarrow")
if installation["metadata"]["version"] != expected:
    raise RuntimeError("arrow_installation_receipt_changed")
print(json.dumps({"scope": "two synthetic queue/read regression checks only",
                  "python": platform.python_version(), "pyarrow": pyarrow.__version__,
                  "wheel_sha256": installation["download_info"]["archive_info"]["hashes"]["sha256"],
                  "isolated_dependency": True, "production_runtime_verified": False}))
raise SystemExit(pytest.main(["-q", "tests/test_research_workbench.py", "-k",
    "changed_code_requires_a_separate_frozen_version or interrupted_process_does_not_claim_success"]))
