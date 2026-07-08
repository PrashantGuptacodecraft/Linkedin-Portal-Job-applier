import pytest
from backend.adapters import get_adapter_for_url, GreenhouseAdapter, LeverAdapter, WorkdayAdapter, GenericSemanticAdapter

def test_get_adapter_for_url():
    # Greenhouse
    assert isinstance(get_adapter_for_url("https://boards.greenhouse.io/company/jobs/123"), GreenhouseAdapter)
    
    # Lever
    assert isinstance(get_adapter_for_url("https://jobs.lever.co/company/123"), LeverAdapter)
    
    # Workday
    assert isinstance(get_adapter_for_url("https://company.myworkdayjobs.com/en-US/careers"), WorkdayAdapter)
    
    # Generic
    assert isinstance(get_adapter_for_url("https://careers.google.com/jobs"), GenericSemanticAdapter)
