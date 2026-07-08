import pytest
import json
from pathlib import Path
from backend.gemini_fallback import GeminiFallback
from backend.models import ExtractedField
import backend.database as db
from backend import config

TEST_DB_DIR = Path(config.DATA_DIR) / "test_data_gf"
TEST_DB_PATH = TEST_DB_DIR / "app.db"

@pytest.fixture(autouse=True)
def setup_teardown(monkeypatch):
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    original_db_path = db.DB_PATH
    db.DB_PATH = TEST_DB_PATH
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
        
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
        
    db.init_db()
    
    import google.generativeai as genai
    import backend.config as cfg
    monkeypatch.setattr(cfg, "get_current_gemini_key", lambda: "fake_key")
    monkeypatch.setattr(genai, "configure", lambda **kwargs: None)
    
    yield
    
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        delattr(db._local, "conn")
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    db.DB_PATH = original_db_path
from backend.gemini_fallback import GeminiFallback
from backend.models import ExtractedField

class MockResponse:
    def __init__(self, text):
        self.text = text

class MockModel:
    def __init__(self, fail_count=0, mock_text=""):
        self.fail_count = fail_count
        self.calls = 0
        self.mock_text = mock_text
        
    def generate_content(self, prompt):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise Exception("API Error")
        return MockResponse(self.mock_text)



def test_redact_secrets():
    profile = {
        "first_name": "Bob",
        "ssn": "123-45-6789",
        "portal_password": "my_secret_password",
        "linkedin_password": "password123"
    }
    safe = GeminiFallback._redact_secrets(profile)
    assert "first_name" in safe
    assert "ssn" not in safe
    assert "portal_password" not in safe
    assert "linkedin_password" not in safe

def test_extract_json():
    # Plain JSON
    assert GeminiFallback._extract_json_from_response('[{"a":1}]') == '[{"a":1}]'
    # Markdown JSON
    md = "Here is your JSON:\n```json\n[{\"a\":1}]\n```"
    assert GeminiFallback._extract_json_from_response(md) == '[{"a":1}]'
    # Markdown plain
    md2 = "```\n[{\"a\":1}]\n```"
    assert GeminiFallback._extract_json_from_response(md2) == '[{"a":1}]'

def test_gemini_success(monkeypatch):
    import google.generativeai as genai
    
    fields = [ExtractedField(field_id="f1", selector="#f1", tag="input", label="Favorite Animal")]
    profile = {"favorite_animal": "Dog"}
    
    expected_json = '[{"field_id": "f1", "action": "fill", "value": "Dog", "confidence": 0.95, "reason": "Profile says Dog"}]'
    monkeypatch.setattr(genai, "GenerativeModel", lambda *args, **kwargs: MockModel(mock_text=expected_json))
    
    # Needs to be a valid task in DB for task_events to have FK
    from backend.models import Task
    db.save_task(Task(task_id="t1", created_at=0, updated_at=0))
    
    decisions = GeminiFallback.resolve_fields("t1", fields, profile, {})
    assert len(decisions) == 1
    assert decisions[0].action == "fill"
    assert decisions[0].value == "Dog"
    assert decisions[0].source == "gemini"

def test_gemini_retry_and_fail(monkeypatch):
    import google.generativeai as genai
    
    fields = [ExtractedField(field_id="f1", selector="#f1", tag="input", label="Unknown")]
    
    # Always fail
    monkeypatch.setattr(genai, "GenerativeModel", lambda *args, **kwargs: MockModel(fail_count=5))
    
    # Needs to be a valid task in DB for task_events to have FK
    from backend.models import Task
    db.save_task(Task(task_id="t2", created_at=0, updated_at=0))
    
    # Should retry once, then return ask_user
    decisions = GeminiFallback.resolve_fields("t2", fields, {}, {})
    assert len(decisions) == 1
    assert decisions[0].action == "ask_user"
    assert decisions[0].source == "gemini_error"
