import pytest
from backend.deterministic_resolver import DeterministicResolver
from backend.models import ExtractedField

@pytest.fixture
def profile():
    return {
        "first_name": "Alice",
        "last_name": "Smith",
        "email": "alice@example.com",
        "phone": "1234567890",
        "linkedin": "linkedin.com/in/alice",
        "work_authorization": "Yes",
        "requires_sponsorship": "No",
        "resume_path": "/path/to/resume.pdf"
    }

def test_resolve_name_fields(profile):
    resolver = DeterministicResolver(profile)
    
    # First name
    f1 = ExtractedField(field_id="1", selector="#fn", tag="input", name="firstName", label="First Name")
    d1 = resolver._resolve_single_field(f1)
    assert d1.action == "fill"
    assert d1.value == "Alice"
    assert d1.confidence >= 0.90
    
    # Last name
    f2 = ExtractedField(field_id="2", selector="#ln", tag="input", aria_label="Last Name")
    d2 = resolver._resolve_single_field(f2)
    assert d2.action == "fill"
    assert d2.value == "Smith"
    
    # Full name
    f3 = ExtractedField(field_id="3", selector="#n", tag="input", label="Full Name")
    d3 = resolver._resolve_single_field(f3)
    assert d3.action == "fill"
    assert d3.value == "Alice Smith"

def test_resolve_sensitive_fields(profile):
    resolver = DeterministicResolver(profile)
    
    # Visa Sponsorship
    f1 = ExtractedField(field_id="1", selector="#sp", tag="select", name="require_sponsorship", label="Will you now or in the future require sponsorship?", options=["Yes", "No"])
    d1 = resolver._resolve_single_field(f1)
    assert d1.action == "select"
    assert d1.value == "No"
    
    # EEO / Diversity
    f2 = ExtractedField(field_id="2", selector="#eeo", tag="select", label="Gender", options=["Male", "Female", "Decline to state"])
    d2 = resolver._resolve_single_field(f2)
    assert d2.action == "select"
    assert d2.value == "Decline to state"
    assert d2.confidence >= 0.90

def test_unresolved_fields(profile):
    resolver = DeterministicResolver(profile)
    
    # Random question not in profile
    f1 = ExtractedField(field_id="1", selector="#q", tag="input", label="What is your favorite color?")
    d1 = resolver._resolve_single_field(f1)
    assert d1 is None  # Should return None so it gets handled by AI or Ask User

    decisions = resolver.resolve_fields([f1])
    assert len(decisions) == 1
    assert decisions[0].action == "ask_user"
    assert decisions[0].confidence == 0.0

def test_invisible_fields(profile):
    resolver = DeterministicResolver(profile)
    
    f1 = ExtractedField(field_id="1", selector="#inv", tag="input", label="First Name", visible=False)
    decisions = resolver.resolve_fields([f1])
    assert len(decisions) == 1
    assert decisions[0].action == "skip"
    assert decisions[0].confidence == 1.0
