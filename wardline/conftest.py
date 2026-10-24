"""
conftest.py (wardline root) -- shared test setup.

1. Puts the wardline/ folder on sys.path so `import backend....` works from
   any test file. backend/ has no __init__.py files (PEP 420 namespace
   packages), so this is the only piece missing.
2. `fake_dynamo` / `seeded_dynamo` fixtures run code with
   DATA_BACKEND=dynamodb against an in-memory fake
   (backend/tests/fake_dynamodb.py), so the Phase 2 DynamoDB path is tested
   without AWS credentials or boto3.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def fake_dynamo(monkeypatch):
    """DATA_BACKEND=dynamodb backed by an empty in-memory resource."""
    from backend.shared import db
    from backend.tests.fake_dynamodb import FakeDynamoResource

    resource = FakeDynamoResource()
    monkeypatch.setenv("DATA_BACKEND", "dynamodb")
    db.set_resource(resource)
    yield resource
    db.set_resource(None)


@pytest.fixture
def seeded_dynamo(fake_dynamo):
    """fake_dynamo loaded by scripts/seed.py itself, so every use also exercises the seed path."""
    spec = importlib.util.spec_from_file_location("wardline_seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    assert seed.seed_all() == 0  # 0 failed tables
    return fake_dynamo