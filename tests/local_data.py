"""Helper for tests marked needs_data, which read local data that CI does not have.

Such a test imports model and data code inside the test, never at module level: pytest imports every test
module before it deselects any, and the model code needs files that CI lacks.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def require_local_data(*relative_paths):
    """Paths under the repository root; fails, naming every missing one, instead of skipping."""
    missing = [path for path in relative_paths if not (REPO_ROOT / path).exists()]
    if missing:
        pytest.fail(f"Local data missing for this needs_data test: {missing}", pytrace=False)
    return [REPO_ROOT / path for path in relative_paths]
