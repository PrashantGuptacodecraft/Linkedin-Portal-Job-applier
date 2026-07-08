"""
form_extractor.py – Scans the DOM to extract structured form fields for AI or deterministic filling.
"""
import uuid
from typing import List

try:
    from .models import ExtractedField
except ImportError:
    from models import ExtractedField

class FormExtractor:
    @staticmethod
    async def extract_fields(page) -> List[ExtractedField]:
        """
        Executes JavaScript within the page to extract all interactable form elements,
        their labels, options, visibility, and constraints.
        """
        js_code = """
        () => {
            function getElementSelector(el) {
                if (el.id) return `#${CSS.escape(el.id)}`;
                if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
                
                let path = [];
                let current = el;
                while (current && current.nodeType === Node.ELEMENT_NODE) {
                    let selector = current.nodeName.toLowerCase();
                    if (current.id) {
                        selector += `#${CSS.escape(current.id)}`;
                        path.unshift(selector);
                        break;
                    } else {
                        let sibling = current;
                        let nth = 1;
                        while (sibling.previousElementSibling) {
                            sibling = sibling.previousElementSibling;
                            if (sibling.nodeName.toLowerCase() == selector) nth++;
                        }
                        if (nth != 1) selector += `:nth-of-type(${nth})`;
                    }
                    path.unshift(selector);
                    current = current.parentNode;
                }
                return path.join(" > ");
            }

            function getLabelText(el) {
                function extractTextWithoutInputs(labelEl) {
                    let clone = labelEl.cloneNode(true);
                    let elementsToRemove = clone.querySelectorAll('input, select, textarea, button');
                    elementsToRemove.forEach(n => n.remove());
                    return clone.innerText.trim();
                }

                if (el.labels && el.labels.length > 0) {
                    return Array.from(el.labels).map(l => extractTextWithoutInputs(l)).join(" ").trim();
                }
                if (el.id) {
                    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (label) return extractTextWithoutInputs(label);
                }
                let parent = el.parentElement;
                while (parent) {
                    if (parent.tagName.toLowerCase() === 'label') {
                        return extractTextWithoutInputs(parent);
                    }
                    parent = parent.parentElement;
                }
                return "";
            }

            function isElementVisible(el) {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }

            const elements = Array.from(document.querySelectorAll('input, textarea, select'));
            const extracted = [];
            
            // Group radios and checkboxes by name to collect options
            const radioGroups = {};
            
            for (const el of elements) {
                if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') continue;
                
                const tag = el.tagName.toLowerCase();
                const type = el.type ? el.type.toLowerCase() : null;
                const name = el.name || "";
                
                if (type === 'radio' && name) {
                    if (!radioGroups[name]) radioGroups[name] = [];
                    const val = el.value || getLabelText(el) || "";
                    if (val) radioGroups[name].push(val);
                    continue; // Process groups at the end
                }

                let options = [];
                if (tag === 'select') {
                    options = Array.from(el.options).map(o => o.innerText.trim()).filter(t => t);
                }

                extracted.push({
                    selector: getElementSelector(el),
                    tag: tag,
                    input_type: type,
                    label: getLabelText(el),
                    aria_label: el.getAttribute('aria-label') || "",
                    placeholder: el.placeholder || "",
                    name: name,
                    id: el.id || "",
                    required: el.required || el.getAttribute('aria-required') === 'true',
                    options: options,
                    nearby_text: el.parentElement ? el.parentElement.innerText.substring(0, 50).trim() : "",
                    visible: isElementVisible(el),
                    value: el.value || ""
                });
            }
            
            // Re-inject radios as single fields with options
            for (const [name, opts] of Object.entries(radioGroups)) {
                const firstRadio = document.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]`);
                if (firstRadio) {
                    extracted.push({
                        selector: `input[type="radio"][name="${CSS.escape(name)}"]`,
                        tag: "input",
                        input_type: "radio",
                        label: getLabelText(firstRadio) || name,
                        aria_label: firstRadio.getAttribute('aria-label') || "",
                        placeholder: "",
                        name: name,
                        id: firstRadio.id || "",
                        required: firstRadio.required || firstRadio.getAttribute('aria-required') === 'true',
                        options: opts,
                        nearby_text: firstRadio.parentElement ? firstRadio.parentElement.innerText.substring(0, 50).trim() : "",
                        visible: isElementVisible(firstRadio),
                        value: ""
                    });
                }
            }

            return extracted;
        }
        """
        
        raw_fields = await page.evaluate(js_code)
        
        results = []
        for raw in raw_fields:
            raw['field_id'] = str(uuid.uuid4())
            results.append(ExtractedField(**raw))
            
        return results

form_extractor = FormExtractor()
