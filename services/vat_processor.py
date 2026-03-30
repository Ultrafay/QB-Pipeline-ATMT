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
TAX_RATE_5_PCT = "5.0% R"      # Recoverable UAE VAT
TAX_RATE_ZERO  = "0.0% Z"      # Zero-rated / foreign


def _is_uae_trn(trn: str) -> bool:
    """Return True if the TRN looks like a valid UAE TRN (15 digits starting with 100)."""
    if not trn:
        return False
    digits = re.sub(r"\D", "", str(trn))
    return len(digits) == 15 and digits.startswith("100")


def _is_uae_address(address: str) -> bool:
    """Return True if the address contains a UAE emirate or country keyword."""
    if not address:
        return False
    lower = address.lower()
    return any(kw in lower for kw in _UAE_KEYWORDS)


def is_uae_invoice(invoice_data: dict) -> bool:
    """
    Determine if an invoice is from a UAE-based vendor.
    Checks supplier TRN and supplier address.
    """
    trn = str(invoice_data.get("supplier_trn", "") or "").strip()
    address = str(invoice_data.get("supplier_address", "") or "").strip()
    return _is_uae_trn(trn) or _is_uae_address(address)


def process_vat(invoice_data: dict) -> dict:
    """
    Adjust invoice line items and add VAT metadata for QBO bill posting.

    UAE invoices:
      - Keep line amounts as pre-tax
      - Tag each line with '5.0% R' or '0.0% Z' based on per-line tax_percentage
      - Set GlobalTaxCalculation = TaxExcluded so QBO computes tax

    Foreign invoices:
      - Distribute total VAT equally across line items (absorbed into expense)
      - Tag all lines with '0.0% Z'
      - Zero out vat_amount so QBO doesn't track it separately
    """
    uae = is_uae_invoice(invoice_data)
    vat_amount = float(invoice_data.get("vat_amount", 0.0) or 0.0)
    line_items: List[dict] = invoice_data.get("line_items", []) or []

    print(f"[VAT] {'UAE' if uae else 'Foreign'} invoice — VAT: {vat_amount}, Lines: {len(line_items)}")

    if uae:
        # ── UAE: keep pre-tax amounts, assign per-line tax codes ──
        for item in line_items:
            tax_pct = item.get("tax_percentage")
            if tax_pct is not None and float(tax_pct) > 0:
                item["qbo_tax_code"] = TAX_RATE_5_PCT
            elif tax_pct is not None and float(tax_pct) == 0:
                item["qbo_tax_code"] = TAX_RATE_ZERO
            else:
                # tax_percentage is null — infer from invoice-level vat_amount
                item["qbo_tax_code"] = TAX_RATE_5_PCT if vat_amount > 0 else TAX_RATE_ZERO

        invoice_data["line_items"] = line_items
        invoice_data["is_uae_invoice"] = True
        invoice_data["apply_global_tax"] = True
        # vat_amount stays as-is for TxnTaxDetail.TotalTax

    else:
        # ── Foreign: absorb VAT into line amounts ──
        valid_lines = [li for li in line_items if float(li.get("amount", 0) or 0) > 0]
        num_lines = len(valid_lines) or 1
        vat_per_line = round(vat_amount / num_lines, 2) if vat_amount > 0 else 0.0

        if vat_amount > 0:
            print(f"[VAT] Distributing {vat_amount} across {num_lines} lines ({vat_per_line}/line)")

        for item in line_items:
            item_amount = float(item.get("amount", 0) or 0)
            if item_amount > 0 and vat_per_line > 0:
                item["amount"] = round(item_amount + vat_per_line, 2)
            item["qbo_tax_code"] = TAX_RATE_ZERO

        invoice_data["line_items"] = line_items
        invoice_data["is_uae_invoice"] = False
        invoice_data["apply_global_tax"] = False
        invoice_data["vat_amount"] = 0.0  # VAT absorbed — don't track separately

    return invoice_data
