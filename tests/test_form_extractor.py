import pytest
import asyncio
from backend.form_extractor import FormExtractor
from playwright.async_api import async_playwright

SYNTHETIC_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Form</title></head>
<body>
    <form>
        <!-- Standard text input -->
        <label for="fname">First Name</label>
        <input type="text" id="fname" name="firstname" placeholder="Enter first name" required>

        <!-- Select dropdown -->
        <label>Country
            <select name="country">
                <option value="us">United States</option>
                <option value="ca">Canada</option>
                <option value="uk">United Kingdom</option>
            </select>
        </label>

        <!-- Checkbox -->
        <input type="checkbox" id="terms" name="terms" aria-label="Accept Terms">
        <label for="terms">I accept the terms and conditions</label>

        <!-- Radio group -->
        <div>
            <label>Gender:</label>
            <input type="radio" id="male" name="gender" value="Male"> <label for="male">Male</label>
            <input type="radio" id="female" name="gender" value="Female"> <label for="female">Female</label>
            <input type="radio" id="other" name="gender" value="Other"> <label for="other">Other</label>
        </div>
        
        <!-- Textarea -->
        <label for="cover_letter">Cover Letter</label>
        <textarea id="cover_letter" name="cover_letter"></textarea>
        
        <!-- Hidden input (should be ignored) -->
        <input type="hidden" name="csrf_token" value="12345">
        
        <!-- Submit button (should be ignored) -->
        <input type="submit" value="Submit">
    </form>
</body>
</html>
"""

def test_extract_fields():
    async def run_test():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(SYNTHETIC_HTML)
            
            fields = await FormExtractor.extract_fields(page)
            
            assert len(fields) == 5  # fname, country, terms, gender (grouped), cover_letter
            
            # Check First Name
            fname = next(f for f in fields if f.name == "firstname")
            assert fname.tag == "input"
            assert fname.input_type == "text"
            assert fname.label == "First Name"
            assert fname.placeholder == "Enter first name"
            assert fname.required is True
            assert fname.id == "fname"
            
            # Check Country
            country = next(f for f in fields if f.name == "country")
            assert country.tag == "select"
            assert country.options == ["United States", "Canada", "United Kingdom"]
            assert country.label == "Country"
            
            # Check Terms
            terms = next(f for f in fields if f.name == "terms")
            assert terms.input_type == "checkbox"
            assert terms.aria_label == "Accept Terms"
            
            # Check Gender (Radios)
            gender = next(f for f in fields if f.name == "gender")
            assert gender.input_type == "radio"
            assert set(gender.options) == {"Male", "Female", "Other"}
            
            # Check Cover Letter
            cl = next(f for f in fields if f.name == "cover_letter")
            assert cl.tag == "textarea"
            assert cl.id == "cover_letter"
            
            await browser.close()
            
    asyncio.run(run_test())
