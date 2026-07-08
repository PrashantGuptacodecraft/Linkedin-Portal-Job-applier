"""
deterministic_resolver.py – Rule-based engine mapping candidate profile fields to extracted forms.
"""
import re
from typing import List, Dict, Any, Optional

try:
    from .models import ExtractedField, FillDecision
except ImportError:
    from models import ExtractedField, FillDecision

class DeterministicResolver:
    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile

    def _normalize(self, text: str) -> str:
        if not text:
            return ""
        # Remove punctuation, lowercase
        return re.sub(r'[^a-z0-9\s]', '', text.lower()).strip()

    def _match_keyword(self, field: ExtractedField, keywords: List[str]) -> bool:
        norm_label = self._normalize(field.label)
        norm_name = self._normalize(field.name)
        norm_aria = self._normalize(field.aria_label)
        
        for kw in keywords:
            kw_norm = self._normalize(kw)
            if kw_norm in norm_label or kw_norm in norm_name or kw_norm in norm_aria:
                return True
        return False

    def _find_best_option(self, field: ExtractedField, target_values: List[str]) -> Optional[str]:
        if not field.options:
            return None
            
        target_norms = [self._normalize(tv) for tv in target_values]
        
        # Exact match (normalized)
        for opt in field.options:
            opt_norm = self._normalize(opt)
            if opt_norm in target_norms:
                return opt
                
        # Partial match
        for opt in field.options:
            opt_norm = self._normalize(opt)
            for target_norm in target_norms:
                if target_norm and (target_norm in opt_norm or opt_norm in target_norm):
                    return opt
                    
        return None

    def resolve_fields(self, extracted_fields: List[ExtractedField]) -> List[FillDecision]:
        decisions = []
        
        for field in extracted_fields:
            if not field.visible:
                # Skip invisible fields
                decisions.append(FillDecision(field_id=field.field_id, action="skip", confidence=1.0, source="rule"))
                continue
                
            decision = self._resolve_single_field(field)
            if decision:
                decisions.append(decision)
            else:
                # Unresolved field -> ask user (or leave for gemini fallback later)
                decisions.append(FillDecision(field_id=field.field_id, action="ask_user", confidence=0.0, source="rule", reason="No deterministic rule matched."))
                
        return decisions

    def _resolve_single_field(self, field: ExtractedField) -> Optional[FillDecision]:
        # 1. EEO / Diversity
        if self._match_keyword(field, ["gender", "sex", "race", "ethnicity", "veteran", "disability", "eeo", "diversity"]):
            # Prefer 'Prefer not to answer' if available
            if field.options or field.input_type in ['radio', 'checkbox']:
                best_opt = self._find_best_option(field, ["prefer not to answer", "decline to state", "do not wish to provide", "i don't wish to answer"])
                if best_opt:
                    return FillDecision(field_id=field.field_id, action="select" if field.tag == "select" else "check", value=best_opt, confidence=0.95, source="rule", reason="Prefer not to answer selected for EEO")
            return FillDecision(field_id=field.field_id, action="ask_user", confidence=0.4, source="rule", reason="EEO/Diversity question without clear decline option")

        # 2. First Name
        if self._match_keyword(field, ["first name", "given name"]):
            val = self.profile.get("first_name", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.95, source="rule")
                
        # 3. Last Name
        if self._match_keyword(field, ["last name", "surname", "family name"]):
            val = self.profile.get("last_name", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.95, source="rule")

        # 4. Full Name
        if self._match_keyword(field, ["full name", "name"]):
            first = self.profile.get("first_name", "")
            last = self.profile.get("last_name", "")
            if first or last:
                return FillDecision(field_id=field.field_id, action="fill", value=f"{first} {last}".strip(), confidence=0.90, source="rule")

        # 5. Email
        if self._match_keyword(field, ["email"]) or field.input_type == "email":
            val = self.profile.get("email", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.99, source="rule")

        # 6. Phone
        if self._match_keyword(field, ["phone", "mobile", "telephone", "cell"]) or field.input_type == "tel":
            val = self.profile.get("phone", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.95, source="rule")

        # 7. City/Location
        if self._match_keyword(field, ["city", "location", "address"]):
            val = self.profile.get("city", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.85, source="rule")

        # 8. LinkedIn
        if self._match_keyword(field, ["linkedin"]):
            val = self.profile.get("linkedin", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.95, source="rule")

        # 9. GitHub / Portfolio / Website
        if self._match_keyword(field, ["github", "portfolio", "website"]):
            val = self.profile.get("github", "") or self.profile.get("portfolio", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.90, source="rule")
                
        # 10. Legal / Visa / Sponsorship
        if self._match_keyword(field, ["sponsorship", "visa", "authorized to work", "legally authorized", "work authorization", "require sponsorship"]):
            # Very sensitive, we require an exact mapping in the profile or we pause
            auth_val = self.profile.get("work_authorization", "")
            spon_val = self.profile.get("requires_sponsorship", "")
            
            if self._match_keyword(field, ["require sponsorship"]):
                if spon_val:
                    opt = self._find_best_option(field, [spon_val, "yes" if spon_val.lower() == "yes" else "no"])
                    if opt: return FillDecision(field_id=field.field_id, action="select" if field.tag == "select" else "check", value=opt, confidence=0.90, source="rule")
            
            elif self._match_keyword(field, ["authorized to work", "legally authorized"]):
                if auth_val:
                    opt = self._find_best_option(field, [auth_val, "yes" if auth_val.lower() == "yes" else "no"])
                    if opt: return FillDecision(field_id=field.field_id, action="select" if field.tag == "select" else "check", value=opt, confidence=0.90, source="rule")
            
            return FillDecision(field_id=field.field_id, action="ask_user", confidence=0.4, source="rule", reason="Sensitive legal/visa question needs user review")

        # 11. Salary
        if self._match_keyword(field, ["salary", "compensation", "pay", "rate"]):
            val = self.profile.get("expected_salary", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.85, source="rule")
            return FillDecision(field_id=field.field_id, action="ask_user", confidence=0.4, source="rule", reason="Salary question without profile value")
            
        # 12. Notice Period
        if self._match_keyword(field, ["notice period", "start date"]):
            val = self.profile.get("notice_period", "")
            if val:
                return FillDecision(field_id=field.field_id, action="fill", value=val, confidence=0.85, source="rule")
                
        # 13. File Uploads (Resume/Cover Letter)
        if field.input_type == "file":
            if self._match_keyword(field, ["resume", "cv"]):
                if self.profile.get("resume_path"):
                    return FillDecision(field_id=field.field_id, action="upload", value=self.profile.get("resume_path"), confidence=0.99, source="rule")
            elif self._match_keyword(field, ["cover letter"]):
                if self.profile.get("cover_letter_path"):
                    return FillDecision(field_id=field.field_id, action="upload", value=self.profile.get("cover_letter_path"), confidence=0.90, source="rule")
            return FillDecision(field_id=field.field_id, action="ask_user", confidence=0.0, source="rule", reason="File upload missing in profile")

        return None
