"""
test_form_engine.py – Offline unit tests for the portal form-fill engine.

These run WITHOUT a browser or network. They guard the bugs that previously
made AI form-filling fail:
  • the f-string `exclude={{'resume_path'}}` set-in-set crash (regression test)
  • dropdown/radio option matching (US -> "United States", "Yes" -> radio option)
Run:  .venv/Scripts/python -m pytest tests/test_form_engine.py -q
  or: .venv/Scripts/python tests/test_form_engine.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models import CandidateProfile
from backend.portal import _best_option_match, _norm, _humanize, _resolve_known_value


def test_profile_json_builds_without_crash():
    """Regression: model_dump_json(exclude={'resume_path'}) must not raise.

    The old code wrote this inside an f-string as `exclude={{'resume_path'}}`,
    which Python parses as a set-containing-a-set -> TypeError every call,
    silently breaking every AI field-fill."""
    c = CandidateProfile(name="Prashant Gupta", email="p@x.com", phone="123")
    js = c.model_dump_json(exclude={"resume_path"})
    assert "Prashant Gupta" in js
    assert "resume_path" not in js


def test_option_match_country_synonym():
    opts = ["Canada", "United States", "United Kingdom", "India"]
    assert _best_option_match("US", opts) == "United States"
    assert _best_option_match("USA", opts) == "United States"
    assert _best_option_match("united states of america", opts) == "United States"


def test_option_match_yes_no():
    assert _best_option_match("Yes", ["Yes", "No"]) == "Yes"
    assert _best_option_match("y", ["Yes", "No"]) == "Yes"
    assert _best_option_match("No", ["Yes", "No"]) == "No"


def test_option_match_demographics():
    opts = ["Male", "Female", "Prefer not to say"]
    assert _best_option_match("decline to answer", opts) == "Prefer not to say"


def test_humanize():
    # Splits separators and camelCase into readable words (case preserved).
    assert _humanize("work_auth.usCitizen").lower() == "work auth us citizen"
    assert _humanize("first-name") == "first name"


def test_resolve_known_value_uses_profile_and_defaults():
    c = CandidateProfile(name="Prashant Gupta", email="p@x.com", phone="999",
                         location="Meerut, UP, India",
                         work_authorization="Authorized to work in the US")
    # First name inferred from full name
    v, src = _resolve_known_value({"label": "First Name", "kind": "input"}, c, "APPLICATION_FORM", "", "")
    assert v == "Prashant"
    # Email direct match
    v, _ = _resolve_known_value({"label": "Email Address", "kind": "input"}, c, "APPLICATION_FORM", "", "")
    assert v == "p@x.com"
    # Sponsorship safe default
    v, src = _resolve_known_value({"label": "Do you require visa sponsorship?", "kind": "radio_group"}, c, "APPLICATION_FORM", "", "")
    assert v == "No" and src == "DEFAULT"
    # City from location
    v, _ = _resolve_known_value({"label": "City", "kind": "input"}, c, "APPLICATION_FORM", "", "")
    assert v == "Meerut"


if __name__ == "__main__":
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = 0
    for f in fns:
        try:
            f()
            print(f"PASS  {f.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {f.__name__}: {e}")
        except Exception as e:
            print(f"ERROR {f.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
