"""
VAT Processor — determines UAE vs foreign vendor and adjusts invoice
line items with the correct QBO tax rate code before bill posting.
"""
import re
from typing import List

# Emirates keywords for address matching (case-insensitive)
_UAE_KEYWORDS = [
    "uae", "united arab emirates",
    "dubai", "abu dhabi", "sharjah", "ajman",
    "fujairah", "ras al khaimah", "umm al quwain",
]

# QBO tax rate code names
TAX_RATE_UAE_SR = "SR Standard Rated"
TAX_RATE_UAE_ZR = "ZR Zero Rated"
TAX_RATE_EXEMPT = "EX Exempt"
TAX_RATE_GCC_IG = "IG Intra GCC"
TAX_RATE_FOREIGN_RC = "RC Reverse Charge"
TAX_RATE_NON    = "NON"

# Locational keywords
_UAE_KEYWORDS = [
    "uae", "united arab emirates",
    "dubai", "abu dhabi", "sharjah", "ajman",
    "fujairah", "ras al khaimah", "umm al quwain",
]
_GCC_KEYWORDS = [
    "saudi arabia", "ksa",
    "oman",
    "bahrain",
    "kuwait",
    "qatar"
]


def _is_uae_trn(trn: str) -> bool:
    if not trn:
        return False
    digits = re.sub(r"\D", "", str(trn))
    return len(digits) == 15 and digits.startswith("100")


def get_location_category(invoice_data: dict) -> str:
    """
    Returns 'UAE', 'GCC', or 'Foreign' based on address heuristics.
    """
    trn = str(invoice_data.get("supplier_trn", "") or "").strip()
    address = str(invoice_data.get("supplier_address", "") or "").strip().lower()
    
    if _is_uae_trn(trn):
        return "UAE"
        
    for kw in _UAE_KEYWORDS:
        if kw in address:
            return "UAE"
            
    for kw in _GCC_KEYWORDS:
        if kw in address:
            return "GCC"
            
    return "Foreign"


def process_vat(invoice_data: dict) -> dict:
    """
    Adjust invoice line items and add VAT metadata for QBO bill posting.

    - UAE: SR Standard Rated (5%), ZR Zero Rated (0%), EX Exempt (Exempt or manual fallback NON)
    - GCC: IG Intra GCC
    - Foreign: RC Reverse Charge + RCM Journal Entry logic (handled downstream)
    """
    category = get_location_category(invoice_data)
    vat_amount = float(invoice_data.get("vat_amount", 0.0) or 0.0)
    line_items: List[dict] = invoice_data.get("line_items", []) or []

    print(f"[VAT] Supplier Location: {category} — VAT: {vat_amount}, Lines: {len(line_items)}")
    
    invoice_data["supplier_location_category"] = category
    # Track non-standard cases requiring manual review
    non_standard_flag = False

    if category == "UAE":
        # UAE: apply standard SR, ZR or NON
        for item in line_items:
            tax_pct = item.get("tax_percentage")
            if tax_pct is not None and float(tax_pct) == 5.0:
                item["qbo_tax_code"] = TAX_RATE_UAE_SR
            elif tax_pct is not None and float(tax_pct) == 0.0:
                item["qbo_tax_code"] = TAX_RATE_UAE_ZR
            elif tax_pct is None:
                item["qbo_tax_code"] = TAX_RATE_UAE_SR if vat_amount > 0 else TAX_RATE_UAE_ZR
            else:
                item["qbo_tax_code"] = TAX_RATE_NON
                non_standard_flag = True

        invoice_data["apply_global_tax"] = True
        
    elif category == "GCC":
        # GCC invoices use a single intra GCC reporting code
        for item in line_items:
            item["qbo_tax_code"] = TAX_RATE_GCC_IG
            
        invoice_data["apply_global_tax"] = False
        invoice_data["vat_amount"] = 0.0  # Zero out since code rules handle it internally

    else:
        # Foreign invoices use RC Reverse Charge
        for item in line_items:
            tax_pct = item.get("tax_percentage")
            if tax_pct is not None and float(tax_pct) == 5.0:
                item["qbo_tax_code"] = TAX_RATE_FOREIGN_RC
            else:
                item["qbo_tax_code"] = TAX_RATE_FOREIGN_RC
                
            # If the parser identified actual VAT, note it but don't charge it globally
            if vat_amount > 0 and tax_pct is not None and float(tax_pct) not in (0.0, 5.0):
                non_standard_flag = True
                
        # Zero out the API tax totals because QBO will compute reverse charge itself
        invoice_data["apply_global_tax"] = False
        invoice_data["vat_amount"] = 0.0
        
    if non_standard_flag:
        msg = f"MANUAL REVIEW REQUIRED: Non-standard {category} VAT rate detected."
        existing = invoice_data.get("manual_review_memo", "")
        invoice_data["manual_review_memo"] = f"{existing} | {msg}" if existing else msg
        print(f"[VAT] {msg}")

    # For backward compatibility downstream in legacy modules
    invoice_data["line_items"] = line_items
    invoice_data["is_uae_invoice"] = (category == "UAE")

    return invoice_data

