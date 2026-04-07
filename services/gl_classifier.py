"""
GL Classification Service  (v2 — sheet-driven, per-line, word-boundary matching)

Reads the "GL Mapping" tab from the dedicated Google Sheet and classifies
individual invoice line items into the correct GL account via keyword matching.

Design:
  - Per-line classification  (classify_line() takes a single description)
  - Word-boundary regex       (\b...\b so "art" won't match "cartridge")
  - Case-insensitive          always
  - Priority ordering         lower number = higher priority
  - 5-minute TTL cache        refreshed automatically on each classify call
  - Pending Review logging    one row PER unmatched line item (not per invoice)
  - Startup validation        cross-checks every GL name in the sheet against
                              the QBO Chart of Accounts and logs warnings

Sheet layout (GL Mapping tab):
  Col A — Keywords   (comma-separated, lowercase)
  Col B — GL Account Name  (exact match to QBO Chart of Accounts)
  Col C — Account Type     (ignored by pipeline — user reference only)
  Col D — Detail Type      (ignored by pipeline — user reference only)
  Col E — Priority         (integer; lower = checked first)
"""

import re
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

# ── Sheet tab names ────────────────────────────────────────────────────────────
GL_MAPPING_TAB     = "GL Mapping"
PENDING_REVIEW_TAB = "Pending Review"

# ── Column indices in GL Mapping tab (0-based) ────────────────────────────────
COL_KEYWORDS = 0   # A: comma-separated keywords
COL_GL_NAME  = 1   # B: GL Account Name (exact QBO match)
# C (index 2) and D (index 3) are user-facing reference — skipped
COL_PRIORITY = 4   # E: Priority integer

# ── Fallback GL account name when no rule matches ────────────────────────────
FALLBACK_GL_NAME = "Uncategorized Expense"

# ── Cache TTL ────────────────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 300   # 5 minutes


class GLClassifier:
    """
    Classifies individual invoice line item descriptions against keyword
    rules stored in a Google Sheet.

    Lifecycle:
        clf = GLClassifier(sheets_service, mapping_sheet_id)
        clf.validate_against_accounts(qbo_account_names)  # startup check

        gl_name, keyword = clf.classify_line(description)
        if gl_name:
            account_ref = accounts_map[gl_name]
        else:
            clf.log_pending_review_line(line_item, invoice_data)
            account_ref = accounts_map[FALLBACK_GL_NAME]
    """

    def __init__(self, sheets_service, mapping_sheet_id: str):
        """
        Args:
            sheets_service:    Authenticated GoogleSheetsService instance
                               (exposes .sheet = service.spreadsheets()).
            mapping_sheet_id:  Spreadsheet ID of the GL Mapping workbook.
                               This is NOT the same as GOOGLE_SHEET_ID
                               (the invoice tracker).
        """
        self._sheets   = sheets_service
        self._sheet_id = mapping_sheet_id

        self._mapping_cache: Optional[List[dict]] = None
        self._cache_fetched_at: Optional[datetime] = None

        print(f"[GL] GLClassifier initialised — sheet: {mapping_sheet_id}")

    # ── Cache management ──────────────────────────────────────────────────────

    def _cache_is_fresh(self) -> bool:
        """Return True if the in-memory cache is still within its TTL."""
        if self._mapping_cache is None or self._cache_fetched_at is None:
            return False
        age = (datetime.now() - self._cache_fetched_at).total_seconds()
        return age < CACHE_TTL_SECONDS

    def load_mapping(self) -> None:
        """
        Fetch the GL Mapping tab and cache it sorted by Priority ascending.
        Safe to call multiple times — re-fetches only when the TTL has expired.
        """
        try:
            result = self._sheets.sheet.values().get(
                spreadsheetId=self._sheet_id,
                range=f"{GL_MAPPING_TAB}!A:E",
            ).execute()
            rows = result.get("values", [])

            if not rows:
                print("[GL] Warning: GL Mapping tab is empty.")
                self._mapping_cache   = []
                self._cache_fetched_at = datetime.now()
                return

            # Row 1 is the header — skip it
            data_rows = rows[1:] if len(rows) > 1 else []

            parsed: List[dict] = []
            for row in data_rows:
                # Pad to at least 5 columns so index access is safe
                while len(row) < 5:
                    row.append("")

                keywords_raw = row[COL_KEYWORDS].strip().lower()
                gl_name      = row[COL_GL_NAME].strip()
                priority_raw = row[COL_PRIORITY].strip()

                if not keywords_raw or not gl_name:
                    continue   # skip incomplete rows

                try:
                    priority = int(priority_raw) if priority_raw else 999
                except ValueError:
                    priority = 999

                # Split on commas and strip individual keywords
                keywords = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()]
                # Pre-compile a regex pattern for each keyword (word-boundary, case-insensitive)
                patterns = []
                for kw in keywords:
                    try:
                        patterns.append(re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
                    except re.error:
                        pass   # skip malformed keywords

                if not patterns:
                    continue

                parsed.append({
                    "keywords": keywords,
                    "patterns": patterns,
                    "gl_name":  gl_name,
                    "priority": priority,
                })

            parsed.sort(key=lambda r: r["priority"])
            self._mapping_cache    = parsed
            self._cache_fetched_at = datetime.now()
            print(
                f"[GL] Loaded {len(parsed)} GL mapping rule(s) "
                f"(TTL={CACHE_TTL_SECONDS}s)"
            )

        except Exception as exc:
            print(f"[GL] Failed to load GL mapping: {exc}")
            # Keep stale cache rather than wiping it — better than nothing
            if self._mapping_cache is None:
                self._mapping_cache = []

    def refresh(self) -> None:
        """Force-expire the cache so the next classify call fetches fresh data."""
        self._cache_fetched_at = None
        self.load_mapping()

    def _ensure_fresh(self) -> None:
        """Load/refresh cache if stale."""
        if not self._cache_is_fresh():
            print("[GL] Cache stale or empty — refreshing from sheet…")
            self.load_mapping()

    # ── Classification ────────────────────────────────────────────────────────

    def classify_line(self, description: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Match a single line item description against the GL mapping rules.

        Uses word-boundary regex so partial-word substrings do not match
        (e.g. keyword "art" will NOT match "cartridge").

        Args:
            description: The line item's description text.

        Returns:
            (gl_account_name, matched_keyword) on a match, or (None, None).
        """
        self._ensure_fresh()

        if not self._mapping_cache:
            print("[GL] No mapping rules — skipping classification.")
            return None, None

        if not description or not description.strip():
            return None, None

        desc = description.strip()

        for rule in self._mapping_cache:
            for kw, pattern in zip(rule["keywords"], rule["patterns"]):
                if pattern.search(desc):
                    print(
                        f"[GL] '{desc[:80]}' → keyword='{kw}' "
                        f"→ GL='{rule['gl_name']}' (priority={rule['priority']})"
                    )
                    return rule["gl_name"], kw

        print(f"[GL] No match for: '{desc[:80]}'")
        return None, None

    # ── Pending Review logging ────────────────────────────────────────────────

    def log_pending_review_line(self, line_item: dict, invoice_data: dict) -> bool:
        """
        Append ONE row to the Pending Review tab for a single unmatched line.

        Pending Review columns (A–H):
            A  Timestamp
            B  Invoice Number
            C  Vendor Name
            D  Line Item Description
            E  Amount
            F  Currency
            G  Suggested Keyword   (blank — user fills in)
            H  Suggested GL Account (blank — user fills in)
        """
        try:
            description = str(line_item.get("description") or "").strip()
            amount      = str(line_item.get("amount") or "").strip()
            currency    = str(
                invoice_data.get("currency") or line_item.get("currency") or ""
            ).strip()

            row = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),        # A: Timestamp
                str(invoice_data.get("invoice_number") or ""),        # B: Invoice Number
                str(invoice_data.get("supplier_name")  or ""),        # C: Vendor Name
                description,                                          # D: Line Description
                amount,                                               # E: Amount
                currency,                                             # F: Currency
                "",                                                   # G: Suggested Keyword
                "",                                                   # H: Suggested GL Account
            ]

            self._sheets.sheet.values().append(
                spreadsheetId=self._sheet_id,
                range=f"{PENDING_REVIEW_TAB}!A:H",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()

            print(
                f"[GL] Pending Review: Invoice={invoice_data.get('invoice_number', 'N/A')} "
                f"Vendor={invoice_data.get('supplier_name', 'N/A')} "
                f"Line='{description[:60]}'"
            )
            return True

        except Exception as exc:
            print(f"[GL] Failed to log Pending Review row: {exc}")
            return False

    # ── Startup validation ────────────────────────────────────────────────────

    def validate_against_accounts(self, account_names: List[str]) -> None:
        """
        Cross-check every GL Account Name in the mapping sheet against the
        live QBO Chart of Accounts.  Logs a WARNING for any name that cannot
        be found so the user can fix typos before invoices start failing.

        Args:
            account_names: List of account display names from QBO
                           (as returned by QuickBooksService.get_all_account_names()).
        """
        self._ensure_fresh()

        if not self._mapping_cache:
            print("[GL] validate_against_accounts: no rules loaded — skipping.")
            return

        known = {n.lower().strip() for n in account_names}
        gl_names_in_sheet = {rule["gl_name"] for rule in self._mapping_cache}

        missing = []
        for gl_name in sorted(gl_names_in_sheet):
            if gl_name.lower().strip() not in known:
                missing.append(gl_name)

        total = len(gl_names_in_sheet)
        found = total - len(missing)
        print(
            f"[GL] Startup validation: {found}/{total} GL account names "
            f"matched in QBO Chart of Accounts."
        )
        for name in missing:
            print(
                f"[GL] WARNING — GL name '{name}' in sheet NOT found in QBO. "
                f"Fix the spelling or add it to the Chart of Accounts."
            )
