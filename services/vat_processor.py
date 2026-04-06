"""
VAT Processor — validates and finalises per-line tax codes before QBO
bill posting.

The AI extractor assigns a `tax_code` (SR / EX / ZR / RC / IG) to each
line item.  This module:

  1. Determines the supplier location category (UAE / GCC / Foreign) from
     TRN + address — used as a safety net.
  2. Validates each line's `tax_code` against the location. If the code is
     missing or inconsistent, it assigns a sensible fallback and flags for
     review.
  3. Maps shorthand codes to the full QBO TaxCode names.
  4. For non-UAE invoices with foreign tax, distributes the tax into line
     amounts and sets a flag for TaxInclusive mode.
  5. Computes implied tax totals from per-line codes and compares them to
     the invoice-level tax amount. Flags mismatches > threshold.
"""
import re
from typing import List

# ── Shorthand → full QBO TaxCode name ─────────────────────────────────────
TAX_CODE_MAP = {
    "SR": "SR Standard Rated",
    "EX": "EX Exempt",
    "ZR": "ZR Zero Rated",
    "RC": "RC Reverse Charge",
    "IG": "IG Intra GCC",
}

# Tax rates implied by each code (used for mismatch validation)
TAX_RATE_MAP = {
    "SR": 0.05,
    "EX": 0.0,
    "ZR": 0.0,
    "RC": 0.0,
    "IG": 0.0,
}

VALID_CODES = set(TAX_CODE_MAP.keys())

# Mismatch threshold (in invoice currency units)
_MISMATCH_THRESHOLD = 1.0

# ── Location keywords ─────────────────────────────────────────────────────
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
    "qatar",
]


def _is_uae_trn(trn: str) -> bool:
    if not trn:
        return False
    digits = re.sub(r"\D", "", str(trn))
    return len(digits) == 15 and digits.startswith("100")


def get_location_category(invoice_data: dict) -> str:
    """Returns 'UAE', 'GCC', or 'Foreign' based on TRN / address heuristics."""
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


# ── Per-line validation helpers ───────────────────────────────────────────

def _valid_codes_for_location(category: str) -> set:
    """Return the set of tax codes that are valid for a given location."""
    if category == "UAE":
        return {"SR", "EX", "ZR"}
    elif category == "GCC":
        return {"IG"}
    else:  # Foreign
        return {"RC"}


def _fallback_code_for_location(category: str, tax_pct, has_invoice_vat: bool) -> str:
    """
    Pick a sensible fallback when the extractor didn't provide a tax_code
    or provided an invalid one.
    """
    if category == "GCC":
        return "IG"
    if category == "Foreign":
        return "RC"
    # UAE — use tax_percentage hint if available
    if tax_pct is not None:
        pct = float(tax_pct)
        if pct == 5.0:
            return "SR"
        if pct == 0.0:
            return "EX"
    # No percentage hint — guess from invoice-level VAT
    return "SR" if has_invoice_vat else "EX"


# ── Foreign tax distribution ──────────────────────────────────────────────

def _determine_rcm_tax(invoice_data: dict, subtotal: float) -> tuple[float, float]:
    """
    Determine the actual RCM tax percentage and absolute amount.
    Returns (percentage, amount)
    """
    tax_pct = invoice_data.get("invoice_tax_percentage")
    tax_amt = float(invoice_data.get("invoice_tax_amount", 0.0) or 0.0)
    
    if tax_pct is not None and tax_pct != "":
        tax_pct = float(tax_pct)
        if tax_amt <= 0 and subtotal > 0:
            tax_amt = round(subtotal * (tax_pct / 100.0), 2)
        return tax_pct, tax_amt
        
    if tax_amt > 0 and subtotal > 0:
        calculated_pct = round((tax_amt / subtotal) * 100.0, 2)
        return calculated_pct, tax_amt
        
    return 0.0, 0.0

def _distribute_foreign_tax(invoice_data: dict, line_items: List[dict]) -> List[dict]:
    """
    For non-UAE invoices, distribute the foreign tax equally across line items.
    
    After distribution, each line's `amount` becomes the gross (tax-inclusive) value.
    """
    subtotal = sum(float(item.get("amount", 0.0) or 0.0) for item in line_items)
    
    rcm_pct, rcm_amt = _determine_rcm_tax(invoice_data, subtotal)
    
    # Store these back so QBO integration can use the exact amount for its journal entry
    invoice_data["rcm_tax_percentage"] = rcm_pct
    invoice_data["rcm_tax_amount"] = rcm_amt
    
    if rcm_amt <= 0:
        return line_items

    valid_line_indices = [i for i, item in enumerate(line_items) if float(item.get("amount", 0.0) or 0.0) > 0]
    num_valid = len(valid_line_indices)
    
    if num_valid == 0:
        return line_items

    print(f"[VAT] Distributing {rcm_amt} (implied {rcm_pct}%) equally across {num_valid} lines (subtotal={subtotal})")

    grossed_up = []
    distributed_total = 0.0
    per_line_tax = round(rcm_amt / num_valid, 2)
    
    valid_count = 0

    for i, item in enumerate(line_items):
        item_amount = float(item.get("amount", 0.0) or 0.0)
        item = dict(item)  # shallow copy
        
        if item_amount <= 0:
            grossed_up.append(item)
            continue
            
        valid_count += 1
        
        if valid_count == num_valid:
            # Last valid line — swallow any rounding remainder cents
            tax_portion = round(rcm_amt - distributed_total, 2)
        else:
            tax_portion = per_line_tax
            
        new_amount = round(item_amount + tax_portion, 2)
        distributed_total += tax_portion

        item["amount"] = new_amount
        item["_pre_tax_amount"] = item_amount  # keep for debugging
        grossed_up.append(item)

    return grossed_up


# ── Main entry point ─────────────────────────────────────────────────────

def process_vat(invoice_data: dict) -> dict:
    """
    Validate per-line tax codes, assign fallbacks where missing, map to
    full QBO names, handle foreign tax distribution, and run tax-total
    mismatch check.
    """
    category = get_location_category(invoice_data)
    vat_amount = float(invoice_data.get("vat_amount", 0.0) or 0.0)
    invoice_tax = float(invoice_data.get("invoice_tax_amount", 0.0) or 0.0)
    line_items: List[dict] = invoice_data.get("line_items", []) or []
    has_invoice_vat = vat_amount > 0 or invoice_tax > 0

    print(f"[VAT] Supplier Location: {category} — VAT: {vat_amount}, Invoice Tax: {invoice_tax}, Lines: {len(line_items)}")

    invoice_data["supplier_location_category"] = category
    valid_codes = _valid_codes_for_location(category)
    review_messages: List[str] = []

    # ── Validate / assign per-line codes ──────────────────────────────────
    for idx, item in enumerate(line_items, start=1):
        raw_code = str(item.get("tax_code", "") or "").upper().strip()

        if raw_code in VALID_CODES:
            # Code is syntactically valid — check it fits the location
            if raw_code not in valid_codes:
                # Mismatch: e.g. extractor said "SR" for a Foreign vendor
                fallback = _fallback_code_for_location(
                    category, item.get("tax_percentage"), has_invoice_vat
                )
                review_messages.append(
                    f"Line {idx}: tax_code '{raw_code}' invalid for {category} vendor, "
                    f"overridden to '{fallback}'"
                )
                raw_code = fallback
        else:
            # Missing or unrecognised code — assign fallback
            fallback = _fallback_code_for_location(
                category, item.get("tax_percentage"), has_invoice_vat
            )
            if raw_code:
                review_messages.append(
                    f"Line {idx}: unrecognised tax_code '{raw_code}', "
                    f"defaulted to '{fallback}'"
                )
            raw_code = fallback

        # Write the validated shorthand back and the full QBO name
        item["tax_code"] = raw_code
        item["qbo_tax_code"] = TAX_CODE_MAP[raw_code]

    # ── Foreign tax distribution (non-UAE only) ───────────────────────────
    if category in ("GCC", "Foreign"):
        line_items = _distribute_foreign_tax(invoice_data, line_items)
        if invoice_data.get("rcm_tax_amount", 0.0) > 0:
            invoice_data["tax_inclusive"] = True
            print(f"[VAT] Foreign tax distributed. Bill will use TaxInclusive mode.")
        else:
            invoice_data["tax_inclusive"] = False
    else:
        invoice_data["tax_inclusive"] = False

    # ── Tax mismatch validation ───────────────────────────────────────────
    # Use invoice_tax_amount (the actual tax on the invoice) for comparison.
    # Fall back to vat_amount if invoice_tax_amount is not set.
    reference_tax = invoice_tax if invoice_tax > 0 else vat_amount

    if category == "UAE":
        # UAE: sum of implied tax from per-line codes
        implied_tax = 0.0
        for item in line_items:
            item_amount = float(item.get("_pre_tax_amount", item.get("amount", 0.0)) or 0.0)
            rate = TAX_RATE_MAP.get(item.get("tax_code", ""), 0.0)
            implied_tax += item_amount * rate

        implied_tax = round(implied_tax, 2)
        diff = abs(implied_tax - reference_tax)

        if diff > _MISMATCH_THRESHOLD:
            msg = (
                f"TAX MISMATCH: computed tax = {implied_tax}, "
                f"invoice tax = {reference_tax}, diff = {diff:.2f} — review line tax codes"
            )
            review_messages.append(msg)
            print(f"[VAT] {msg}")
    else:
        # Non-UAE: verify grossed-up total equals invoice total
        total_amount = float(invoice_data.get("total_amount", 0.0) or 0.0)
        # Use rcm_tax_amount. Only check if there was tax.
        assigned_tax = invoice_data.get("rcm_tax_amount", 0.0)
        if total_amount > 0 and assigned_tax > 0:
            grossed_sum = sum(float(item.get("amount", 0.0) or 0.0) for item in line_items)
            grossed_sum = round(grossed_sum, 2)
            diff = abs(grossed_sum - total_amount)

            if diff > _MISMATCH_THRESHOLD:
                msg = (
                    f"TAX MISMATCH: grossed-up line total = {grossed_sum}, "
                    f"invoice total = {total_amount}, diff = {diff:.2f} — review amounts"
                )
                review_messages.append(msg)
                print(f"[VAT] {msg}")

    # ── Assemble review memo ──────────────────────────────────────────────
    if review_messages:
        combined = " | ".join(review_messages)
        existing_memo = invoice_data.get("manual_review_memo", "") or ""
        invoice_data["manual_review_memo"] = (
            f"{existing_memo} | {combined}" if existing_memo else combined
        )
        print(f"[VAT] Review flagged: {combined}")

    # ── Metadata for downstream consumers ─────────────────────────────────
    invoice_data["line_items"] = line_items
    invoice_data["is_uae_invoice"] = (category == "UAE")

    # For GCC / Foreign WITHOUT foreign tax distribution, zero out vat_amount
    # so QBO doesn't double-count.  If tax was distributed, keep it for audit.
    if category in ("GCC", "Foreign") and not invoice_data.get("tax_inclusive"):
        invoice_data["vat_amount"] = 0.0

    return invoice_data
