"""
GL Classification Service.

Reads the "GL Mapping" tab from a dedicated Google Sheet and classifies
invoice line items into the correct GL account by keyword matching.

Caches the mapping on first use to avoid repeated API calls.
Logs unmatched invoices to the "Pending Review" tab.
"""

from datetime import datetime
from typing import Optional, List

# Tab names (must match actual sheet tab names)
GL_MAPPING_TAB   = "GL Mapping"
PENDING_REVIEW_TAB = "Pending Review"

# Column indices in "GL Mapping" tab (0-based after reading as list)
COL_KEYWORDS  = 0   # A: comma-separated keywords (lowercase)
COL_GL_NAME   = 1   # B: GL Account Name (exact QBO match)
COL_PRIORITY  = 4   # E: Priority (1 = highest, checked first)


class GLClassifier:
    """
    Classifies invoices into GL accounts by matching line item descriptions
    against a keyword-based mapping table stored in Google Sheets.

    Usage:
        clf = GLClassifier(sheets_service, mapping_sheet_id)
        gl_name = clf.classify(invoice_data["line_items"])
        if gl_name:
            # resolve account ref and post bill
        else:
            clf.log_pending_review(invoice_data, "No match")
    """

    def __init__(self, sheets_service, mapping_sheet_id: str):
        """
        Args:
            sheets_service: An initialized GoogleSheetsService instance
                            (provides the authenticated Sheets API client).
            mapping_sheet_id: The spreadsheet ID of the "GL Category Mapping" sheet.
                              This is separate from GOOGLE_SHEET_ID (the invoice tracker).
        """
        self._sheets_service = sheets_service
        self._mapping_sheet_id = mapping_sheet_id
        self._mapping_cache: Optional[List[dict]] = None  # loaded on first use

        print(f"[GL] GLClassifier initialized — sheet: {mapping_sheet_id}")

    # ── Mapping Cache ────────────────────────────────────────────────────────

    def load_mapping(self) -> None:
        """
        Fetch the GL Mapping tab and cache it sorted by Priority (ascending).
        Call explicitly on startup to warm the cache, or let classify() do it lazily.
        """
        try:
            result = self._sheets_service.sheet.values().get(
                spreadsheetId=self._mapping_sheet_id,
                range=f"{GL_MAPPING_TAB}!A:F"
            ).execute()
            rows = result.get("values", [])

            if not rows:
                print("[GL] Warning: GL Mapping tab is empty.")
                self._mapping_cache = []
                return

            # Skip header row (row 1)
            data_rows = rows[1:] if len(rows) > 1 else []

            parsed = []
            for row in data_rows:
                # Pad short rows
                while len(row) < 6:
                    row.append("")

                keywords_raw = row[COL_KEYWORDS].strip().lower()
                gl_name      = row[COL_GL_NAME].strip()
                priority_raw = row[COL_PRIORITY].strip()

                if not keywords_raw or not gl_name:
                    continue  # skip empty / incomplete rows

                try:
                    priority = int(priority_raw) if priority_raw else 999
                except ValueError:
                    priority = 999

                # Split comma-separated keywords, strip whitespace
                keywords = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()]

                parsed.append({
                    "keywords": keywords,
                    "gl_name":  gl_name,
                    "priority": priority,
                })

            # Sort by priority ascending (1 = highest priority = checked first)
            parsed.sort(key=lambda r: r["priority"])
            self._mapping_cache = parsed
            print(f"[GL] Loaded {len(parsed)} GL mapping rule(s).")

        except Exception as e:
            print(f"[GL] Failed to load GL mapping: {e}")
            self._mapping_cache = []

    def refresh(self) -> None:
        """Force a reload of the GL mapping from the sheet."""
        self._mapping_cache = None
        self.load_mapping()

    # ── Classification ───────────────────────────────────────────────────────

    def classify(self, line_items: list) -> Optional[str]:
        """
        Match invoice line item descriptions against the GL mapping rules.

        Args:
            line_items: List of line item dicts, each with a "description" key.

        Returns:
            The GL Account Name string (col B) from the first matching rule,
            or None if no rule matched.
        """
        # Lazy-load on first call
        if self._mapping_cache is None:
            self.load_mapping()

        if not self._mapping_cache:
            print("[GL] No mapping rules available — skipping classification.")
            return None

        # Build a single searchable string from all line item descriptions
        combined = " ".join(
            str(item.get("description") or "").lower()
            for item in (line_items or [])
        ).strip()

        if not combined:
            print("[GL] No line item descriptions to classify.")
            return None

        print(f"[GL] Classifying: '{combined[:120]}{'...' if len(combined) > 120 else ''}'")

        for rule in self._mapping_cache:
            for keyword in rule["keywords"]:
                if keyword and keyword in combined:
                    print(f"[GL] Matched keyword '{keyword}' → GL: '{rule['gl_name']}' (priority={rule['priority']})")
                    return rule["gl_name"]

        print("[GL] No GL mapping match found.")
        return None

    # ── Pending Review Log ───────────────────────────────────────────────────

    def log_pending_review(self, invoice_data: dict, suggested_gl: str = "No match") -> bool:
        """
        Append a row to the "Pending Review" tab when no GL match is found.

        Columns (A–J):
            A: Date Logged
            B: Invoice Number
            C: Vendor
            D: Invoice Date
            E: Amount
            F: Line Item Descriptions (joined)
            G: Suggested GL
            H: Status  = "Pending"
            I: Reviewed By  = ""
            J: Final GL Assigned = ""
        """
        try:
            date_logged = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Join all line item descriptions for column F
            line_items = invoice_data.get("line_items") or []
            descriptions = "; ".join(
                str(item.get("description") or "").strip()
                for item in line_items
                if item.get("description")
            ) or invoice_data.get("description", "")

            row = [
                date_logged,                                             # A: Date Logged
                str(invoice_data.get("invoice_number", "") or ""),       # B: Invoice Number
                str(invoice_data.get("supplier_name", "") or ""),        # C: Vendor
                str(invoice_data.get("date", "") or ""),                 # D: Invoice Date
                str(invoice_data.get("total_amount", "") or ""),         # E: Amount
                descriptions,                                            # F: Line Item Descriptions
                suggested_gl,                                            # G: Suggested GL
                "Pending",                                               # H: Status
                "",                                                      # I: Reviewed By
                "",                                                      # J: Final GL Assigned
            ]

            body = {"values": [row]}
            self._sheets_service.sheet.values().append(
                spreadsheetId=self._mapping_sheet_id,
                range=f"{PENDING_REVIEW_TAB}!A:J",
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()

            print(f"[GL] Logged to Pending Review: Invoice {invoice_data.get('invoice_number', 'N/A')} — '{suggested_gl}'")
            return True

        except Exception as e:
            print(f"[GL] Failed to log to Pending Review tab: {e}")
            return False
