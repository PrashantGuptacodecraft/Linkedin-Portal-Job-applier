"""
gemini_fallback.py – Gemini fallback to answer unresolved fields with strict JSON outputs.
"""
import json
import re
from typing import List, Dict, Any, Optional
import google.generativeai as genai

try:
    from .models import ExtractedField, FillDecision, TaskStatus
    from .config import get_current_gemini_key
    from .task_manager import task_manager
except ImportError:
    from models import ExtractedField, FillDecision, TaskStatus
    from config import get_current_gemini_key
    from task_manager import task_manager

class GeminiFallback:
    @staticmethod
    def _redact_secrets(profile: dict) -> dict:
        """Remove passwords, keys, or SSNs from the profile before sending to LLM."""
        safe = profile.copy()
        sensitive_keys = ["portal_password", "linkedin_password", "password", "ssn", "api_key", "secret"]
        for k in sensitive_keys:
            if k in safe:
                del safe[k]
        return safe

    @staticmethod
    def _extract_json_from_response(text: str) -> str:
        """Extracts JSON array if wrapped in markdown blocks."""
        text = text.strip()
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
        if "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
        return text

    @staticmethod
    def resolve_fields(task_id: str, unresolved_fields: List[ExtractedField], profile: dict, job_details: dict, retry_count: int = 1) -> List[FillDecision]:
        """
        Send unresolved fields to Gemini to get FillDecisions.
        If JSON is invalid, retries up to `retry_count` times.
        If still invalid, returns decisions with 'ask_user'.
        """
        if not unresolved_fields:
            return []

        api_key = get_current_gemini_key()
        if not api_key:
            return [FillDecision(field_id=f.field_id, action="ask_user", confidence=0.0, source="gemini", reason="No API key configured") for f in unresolved_fields]
            
        genai.configure(api_key=api_key)
        # We can use gemini-1.5-flash or gemini-2.5-flash. The prompt strictly asks for JSON.
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        
        safe_profile = GeminiFallback._redact_secrets(profile)
        
        fields_json = [
            {
                "field_id": f.field_id,
                "label": f.label,
                "name": f.name,
                "input_type": f.input_type,
                "tag": f.tag,
                "options": f.options,
                "required": f.required
            } for f in unresolved_fields
        ]

        prompt = f"""
You are an expert recruitment assistant helping to fill out a job application.
I am providing you with the applicant's profile and the job details. 
There are some form fields that we could not automatically map.

Candidate Profile:
{json.dumps(safe_profile, indent=2)}

Job Details:
{json.dumps(job_details, indent=2)}

Unresolved Form Fields:
{json.dumps(fields_json, indent=2)}

Your task is to analyze each unresolved field and determine the best action based ONLY on the Candidate Profile.
Return a valid JSON array of objects. Each object must strictly follow this schema:
{{
    "field_id": "string (MUST exactly match the input field_id)",
    "action": "string (one of: 'fill', 'select', 'check', 'skip', 'ask_user')",
    "value": "string or null (the value to fill/select/check. Null if skipping or asking user)",
    "confidence": "float (0.0 to 1.0)",
    "reason": "string (brief explanation of your decision)"
}}

Rules:
1. If you are extremely confident (>0.9), choose action 'fill', 'select', or 'check' and provide the exact value.
2. If it is a select dropdown or radio buttons, 'value' MUST exactly match one of the provided 'options' in the field.
3. If the profile does not contain the information, or you are unsure, choose action 'ask_user' and value null.
4. Output ONLY the JSON array, no other text.
"""

        for attempt in range(retry_count + 1):
            try:
                response = model.generate_content(prompt)
                raw_text = response.text
                json_str = GeminiFallback._extract_json_from_response(raw_text)
                parsed = json.loads(json_str)
                
                if not isinstance(parsed, list):
                    raise ValueError("Response is not a JSON array")
                
                decisions = []
                for item in parsed:
                    # Validate using Pydantic model
                    item["source"] = "gemini"
                    decisions.append(FillDecision(**item))
                
                return decisions
            except Exception as e:
                task_manager.log_event(task_id, "error", f"Gemini fallback attempt {attempt+1} failed: {str(e)}")
                if attempt == retry_count:
                    # Final fallback: ask user for all
                    return [FillDecision(
                        field_id=f.field_id, 
                        action="ask_user", 
                        confidence=0.0, 
                        source="gemini_error", 
                        reason=f"Failed to parse AI response: {str(e)}"
                    ) for f in unresolved_fields]

gemini_fallback = GeminiFallback()
