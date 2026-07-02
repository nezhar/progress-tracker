import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "adminpw")
    monkeypatch.setattr(app_module, "PARTICIPANT_PASSCODE", "")
    with TestClient(app_module.app) as c:
        yield c
