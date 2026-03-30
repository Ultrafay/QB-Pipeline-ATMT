from openai import OpenAI
import json
import base64
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field
import os

# --- Data Models (shared with the rest of the app) ---

class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None
    tax_percentage: Optional[float] = None  # 0, 5, or null

class InvoiceData(BaseModel):
    date: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_trn: Optional[str] = None
    supplier_address: Optional[str] = None
    invoice_number: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    credit_terms: Optional[str] = None
    bill_to: Optional[str] = None
    bill_to_trn: Optional[str] = None
    gl_code_suggested: Optional[str] = None
    exclusive_amount: Optional[float] = None
    vat_amount: Optional[float] = None
    total_amount: Optional[float] = None
    currency: str = "AED"
    line_items: List[LineItem] = []
    extraction_confidence: str = "medium"
    extraction_method: str = "openai_gpt4o"
    notes: Optional[str] = None
    raw_response: Optional[str] = None

# --- Extractor Class ---

class OpenAIExtractor:
    def __init__(self, api_key: str, org_id: str = None, project_id: str = None):
        if not api_key:
            raise ValueError("OpenAI API key is required")
        
        self.client = OpenAI(
            api_key=api_key,
            organization=org_id,
            project=project_id
        )
        self.model = "gpt-4o"
        
        self.system_prompt = """You are an expert invoice data extraction system for a UAE-based company.

Analyze this invoice and extract ALL relevant data. The invoice may contain English, Arabic, or both languages.

CRITICAL INSTRUCTIONS:
1. EXTRACT EVERY SINGLE LINE ITEM. Do not group them. Do not summarize them.
2. For Arabic company names, provide both Arabic and English transliteration if visible
3. All amounts must be numeric only (no currency symbols, no commas)
4. Dates must be in YYYY-MM-DD format
5. If a field is not visible or unclear, use null
6. TRN (Tax Registration Number) in UAE is 15 digits starting with "100"
7. Extract the exact currency code (e.g., AED, USD, EUR). Default to USD if none is detected.
8. Extract the supplier's full address as a single string.
9. For EACH line item, extract the VAT/tax percentage applied (0, 5, or null if not shown).

IDENTIFY CORRECTLY:
- SUPPLIER = The company SENDING the invoice
- BILL TO = The company RECEIVING the invoice

EXTRACT INTO THIS EXACT JSON STRUCTURE:

{
  "date": "YYYY-MM-DD",
  "supplier_name": "Company issuing the invoice",
  "supplier_trn": "15-digit TRN or null",
  "supplier_address": "Full supplier address as a single string, or null",
  "invoice_number": "Invoice reference number",
  "description": "General description of invoice (optional)",
  "due_date": "YYYY-MM-DD or null",
  "credit_terms": "NET 30, Cheque, Immediate, etc.",
  "bill_to": "Customer name",
  "gl_code_suggested": "Medical Supplies | Equipment | Marketing | Professional Services | Government Fees | Office Supplies | Utilities | Other",
  "exclusive_amount": 0.00,
  "vat_amount": 0.00,
  "total_amount": 0.00,
  "currency": "AED (default to USD if unknown)",
  "line_items": [
    {
      "description": "Exact item name from invoice",
      "quantity": 1.0,
      "unit_price": 0.00,
      "amount": 0.00,
      "tax_percentage": 5
    },
    {
      "description": "Zero-rated item",
      "quantity": 1.0,
      "unit_price": 10.00,
      "amount": 10.00,
      "tax_percentage": 0
    }
  ],
  "extraction_confidence": "high|medium|low",
  "notes": "Any issues found"
}

IMPORTANT:
- If an item is free/bonus (FOC), set unit_price to 0 and amount to 0.
- Capture the EXACT item description for each line.
- The 'amount' in line_items should be the line total (Quantity * Unit Price) BEFORE tax.
- tax_percentage per line: use 5 for 5% VAT, 0 for zero-rated/exempt, null if not visible.
- Ensure 'total_amount' matches the sum of line items + VAT.

Return ONLY valid JSON. No markdown."""

    def _encode_image_to_base64(self, image_path: str) -> str:
        """Read an image file and return its base64 encoding."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _get_mime_type(self, file_path: str) -> str:
        """Determine MIME type from file extension."""
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        return mime_map.get(ext, "image/jpeg")

    def extract_from_image(self, image_path: str) -> InvoiceData:
        """Extract invoice data from an image file using GPT-4o Vision."""
        print(f"OpenAI: Extracting from image {image_path}")
        
        b64_image = self._encode_image_to_base64(image_path)
        mime_type = self._get_mime_type(image_path)
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all invoice data from this image."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=4096,
            temperature=0.1
        )
        
        return self._parse_response(response.choices[0].message.content)

    def extract_from_pdf(self, pdf_path: str) -> InvoiceData:
        """Extract invoice data from PDF by converting pages to images first."""
        print(f"OpenAI: Extracting from PDF {pdf_path}")
        
        from pdf2image import convert_from_path
        import tempfile
        
        # Convert first page of PDF to image
        images = convert_from_path(pdf_path, first_page=1, last_page=1, dpi=200)
        
        if not images:
            raise ValueError("Could not convert PDF to image")
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            images[0].save(tmp.name, "JPEG", quality=95)
            tmp_path = tmp.name
        
        try:
            result = self.extract_from_image(tmp_path)
        finally:
            os.unlink(tmp_path)
        
        return result

    def _parse_response(self, response_text: str) -> InvoiceData:
        """Parse the JSON response from OpenAI into an InvoiceData object."""
        try:
            # Clean up markdown formatting if present
            clean_text = response_text
            if "```json" in clean_text:
                clean_text = clean_text.split("```json")[1].split("```")[0]
            elif "```" in clean_text:
                clean_text = clean_text.split("```")[1].split("```")[0]
            clean_text = clean_text.strip()
            
            data = json.loads(clean_text)
            
            # Default missing/unknown currency to USD
            extracted_currency = str(data.get("currency", "") or "").upper().strip()
            # Keep only the first word in case model returns "AED (default...)" style values
            extracted_currency = extracted_currency.split()[0] if extracted_currency else ""
            # Validate it's a proper 3-letter ISO currency code; fall back to USD otherwise
            if not extracted_currency or len(extracted_currency) != 3 or not extracted_currency.isalpha():
                data["currency"] = "USD"
            else:
                data["currency"] = extracted_currency
            
            invoice = InvoiceData(**data)
            invoice.raw_response = clean_text
            return invoice
        except Exception as e:
            print(f"Error parsing OpenAI response: {e}")
            print(f"Raw response: {response_text}")
            raise ValueError(f"Failed to parse JSON from OpenAI response: {e}")
