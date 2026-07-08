"""
adapters.py – Portal-specific adapters for ATS systems.
"""
import re
from typing import List, Dict, Any, Optional

try:
    from .models import ExtractedField, FillDecision
    from .form_extractor import FormExtractor
    from .deterministic_resolver import DeterministicResolver
except ImportError:
    from models import ExtractedField, FillDecision
    from form_extractor import FormExtractor
    from deterministic_resolver import DeterministicResolver

class BasePortalAdapter:
    URL_PATTERN = ""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        if not cls.URL_PATTERN:
            return False
        return bool(re.search(cls.URL_PATTERN, url, re.IGNORECASE))

    async def fill_form(self, page, profile: dict) -> int:
        """
        Extract fields, resolve them, and fill the page.
        Returns the number of fields filled.
        """
        # 1. Extract
        fields = await FormExtractor.extract_fields(page)
        if not fields:
            return 0
            
        # 2. Resolve
        resolver = DeterministicResolver(profile)
        decisions = resolver.resolve_fields(fields)
        
        # 3. Apply ATS-specific overrides
        decisions = self._apply_overrides(fields, decisions, profile)
        
        # 4. Fill
        filled = 0
        for decision in decisions:
            if decision.action in ["fill", "select", "check"]:
                # find field
                field = next((f for f in fields if f.field_id == decision.field_id), None)
                if field and decision.value:
                    success = await self._execute_fill(page, field, decision.value, decision.action)
                    if success:
                        filled += 1
            elif decision.action == "upload":
                field = next((f for f in fields if f.field_id == decision.field_id), None)
                if field and decision.value:
                    success = await self._execute_upload(page, field, decision.value)
                    if success:
                        filled += 1
        return filled

    def _apply_overrides(self, fields: List[ExtractedField], decisions: List[FillDecision], profile: dict) -> List[FillDecision]:
        return decisions

    async def _execute_fill(self, page, field: ExtractedField, value: str, action: str) -> bool:
        try:
            el = await page.query_selector(field.selector)
            if not el:
                return False
                
            if action == "fill":
                await el.fill(value)
            elif action == "select":
                # For select, we might need to find the option value
                # Playwright select_option works well with exact text or value
                await el.select_option(label=value)
            elif action == "check":
                if value.lower() in ["yes", "true", "1", "on"]:
                    await el.check()
                else:
                    await el.uncheck()
            return True
        except Exception:
            return False
            
    async def _execute_upload(self, page, field: ExtractedField, file_path: str) -> bool:
        try:
            el = await page.query_selector(field.selector)
            if not el:
                return False
            await el.set_input_files(file_path)
            return True
        except Exception:
            return False

class GreenhouseAdapter(BasePortalAdapter):
    URL_PATTERN = r"(boards\.greenhouse\.io|app\.greenhouse\.io)"

class LeverAdapter(BasePortalAdapter):
    URL_PATTERN = r"(jobs\.lever\.co|jobs\.eu\.lever\.co)"

class WorkdayAdapter(BasePortalAdapter):
    URL_PATTERN = r"(myworkdayjobs\.com)"
    
    async def fill_form(self, page, profile: dict) -> int:
        # Workday is heavily dynamic. Just return 0 for now and let the system
        # pause or use AI if needed, or handle login pauses.
        return 0

class GenericSemanticAdapter(BasePortalAdapter):
    URL_PATTERN = r".*"  # Matches anything as a fallback

def get_adapter_for_url(url: str) -> BasePortalAdapter:
    for adapter_class in [GreenhouseAdapter, LeverAdapter, WorkdayAdapter]:
        if adapter_class.can_handle(url):
            return adapter_class()
    return GenericSemanticAdapter()
