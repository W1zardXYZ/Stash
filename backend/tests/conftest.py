import os
import sys
import tempfile
from pathlib import Path

import pytest

# Configure Stash before it is imported: throw-away data dir, no background workers.
_TMP = tempfile.mkdtemp(prefix="stash_test_")
os.environ["STASH_DATA_DIR"] = _TMP
os.environ["STASH_WORKERS"] = "0"
os.environ.setdefault("STASH_RENDER_THUMBNAILS", "false")
if os.environ.get("STASH_TEST_BLENDER") != "1":
    os.environ["STASH_BLENDER"] = "none"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session")
def app_client():
    from fastapi.testclient import TestClient

    from stash.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def run_jobs():
    from stash.jobs import run_pending_sync

    return run_pending_sync
