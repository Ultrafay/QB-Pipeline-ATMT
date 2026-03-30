"""
QuickBooks Online integration module.

Handles:
  - OAuth 2.0 token management with automatic refresh on 401
  - Fuzzy vendor search + auto-creation
  - Bill posting via POST /v3/company/{realm_id}/bill

All tokens are read from and written back to the .env file automatically.
"""

import os
import json
import base64
from datetime import date
from typing import Optional, Tuple, Dict
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from thefuzz import fuzz
from dotenv import load_dotenv, set_key, find_dotenv

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────

SANDBOX_BASE    = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
TOKEN_URL       = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

FUZZY_MATCH_THRESHOLD = 80   # minimum similarity score (0–100) to accept a vendor match


# ── Service Class ────────────────────────────────────────────────────────────

class QuickBooksService:
    """
    Integrates with QuickBooks Online API.

    Usage:
        qbo = QuickBooksService()
        status, bill_id = qbo.sync(invoice_data_dict)
    """

    def __init__(self):
        self.client_id     = os.getenv("QBO_CLIENT_ID", "")
        self.client_secret = os.getenv("QBO_CLIENT_SECRET", "")
        self.realm_id      = os.getenv("QBO_REALM_ID", "")
        self.access_token  = os.getenv("QBO_ACCESS_TOKEN", "")
        self.refresh_token = os.getenv("QBO_REFRESH_TOKEN", "")

        environment  = os.getenv("QBO_ENVIRONMENT", "sandbox").lower()
        self.base_url = SANDBOX_BASE if environment == "sandbox" else PRODUCTION_BASE

        # Path to .env for writing back refreshed tokens.
        # Use an explicit absolute path co-located with this project so token
        # write-back always goes to the right file regardless of cwd.
        _project_root = Path(__file__).resolve().parent.parent  # services/ -> project root
        _explicit_env = _project_root / ".env"
        self._env_path = str(_explicit_env) if _explicit_env.exists() else (find_dotenv() or ".env")
        self._cache_file = _project_root / "vendor_cache.json"
        print(f"[QBO] Token store: {self._env_path}")

        if not self.realm_id:
            raise ValueError("QBO_REALM_ID is not set in .env")
        if not self.client_id or not self.client_secret:
            raise ValueError("QBO_CLIENT_ID / QBO_CLIENT_SECRET not set in .env")

        self.vendor_cache = self._load_vendor_cache()
        self.gl_cache = {}
        self.default_expense_account = None
        self._tax_rate_map = None   # name -> TaxCode ID, populated lazily
        print(f"[QBO] Initialized ({environment}) — realm: {self.realm_id} — cached vendors: {len(self.vendor_cache)}")

    # ── Vendor Cache ─────────────────────────────────────────────────────────

    def _load_vendor_cache(self) -> dict:
        """Load the local vendor mapping cache."""
        if self._cache_file.exists():
            try:
                with open(self._cache_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[QBO] Failed to load vendor cache: {e}")
        return {}

    def _save_vendor_cache(self) -> None:
        """Save the local vendor mapping cache."""
        try:
            with open(self._cache_file, "w") as f:
                json.dump(self.vendor_cache, f, indent=2)
        except Exception as e:
            print(f"[QBO] Failed to save vendor cache: {e}")

    # ── Token Management ─────────────────────────────────────────────────────

    def _save_tokens(self, access_token: str, refresh_token: str) -> None:
        """Persist refreshed tokens back to the .env file."""
        self.access_token  = access_token
        self.refresh_token = refresh_token
        try:
            set_key(self._env_path, "QBO_ACCESS_TOKEN",  access_token)
            set_key(self._env_path, "QBO_REFRESH_TOKEN", refresh_token)
            print("[QBO] Tokens refreshed and saved to .env")
        except Exception as e:
            print(f"[QBO] Warning: could not write tokens to .env: {e}")

    def _do_refresh(self) -> bool:
        """POST to Intuit token endpoint using refresh_token grant."""
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded     = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded}",
            "Content-Type":  "application/x-www-form-urlencoded",
            "Accept":        "application/json",
        }
        data = {
            "grant_type":    "refresh_token",
            "refresh_token": self.refresh_token,
        }

        try:
            resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
            resp.raise_for_status()
            token_data = resp.json()
            self._save_tokens(token_data["access_token"], token_data["refresh_token"])
            return True
        except Exception as e:
            print(f"[QBO] Token refresh failed: {e}")
            return False

    # ── Authenticated Request ────────────────────────────────────────────────

    def _request(self, method: str, endpoint: str, retry: bool = True, **kwargs) -> requests.Response:
        """
        Make an authenticated request to the QBO v3 API.
        Automatically retries once after refreshing the token on 401.
        """
        url = f"{self.base_url}/v3/company/{self.realm_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        headers.update(kwargs.pop("extra_headers", {}))

        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        # Auto-refresh on 401 Unauthorized
        if resp.status_code == 401 and retry:
            print("[QBO] 401 received — refreshing token and retrying...")
            if self._do_refresh():
                headers["Authorization"] = f"Bearer {self.access_token}"
                resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        return resp

    # ── Tax Code Management ──────────────────────────────────────────────────

    def _get_tax_rate_map(self) -> dict:
        """
        Query QBO for active TaxCode objects and build a name → ID map.
        Uses partial name matching: e.g. '5.0% R' matches '5.0% R (5%)'.
        Called lazily on first bill, then cached.
        """
        if self._tax_rate_map is not None:
            return self._tax_rate_map

        self._tax_rate_map = {}
        try:
            query = "SELECT * FROM TaxCode WHERE Active = true MAXRESULTS 100"
            resp = self._request("GET", "query", params={"query": query})

            if resp.status_code != 200:
                print(f"[QBO] TaxCode query failed: {resp.status_code} — {resp.text[:200]}")
                return self._tax_rate_map

            tax_codes = resp.json().get("QueryResponse", {}).get("TaxCode", [])
            print(f"[QBO] Found {len(tax_codes)} active TaxCode(s):")

            for tc in tax_codes:
                tc_id   = str(tc.get("Id", ""))
                tc_name = tc.get("Name", "")
                self._tax_rate_map[tc_name] = tc_id
                print(f"[QBO]   TaxCode '{tc_name}' -> ID {tc_id}")

        except Exception as e:
            print(f"[QBO] _get_tax_rate_map error: {e}")

        return self._tax_rate_map

    def _resolve_tax_code_by_name(self, name: str) -> dict:
        """
        Resolve a tax rate display name (e.g. '5.0% R') to a QBO TaxCodeRef.
        Uses partial matching: '5.0% R' matches '5.0% R (5%)'.
        Falls back to first available code if no match found.
        """
        rate_map = self._get_tax_rate_map()

        # Exact match first
        if name in rate_map:
            return {"value": rate_map[name]}

        # Partial match (name is a prefix of the TaxCode name)
        for tc_name, tc_id in rate_map.items():
            if tc_name.startswith(name) or name in tc_name:
                return {"value": tc_id}

        # Fallback: "NON" if nothing found
        print(f"[QBO] Warning: Tax code '{name}' not found in QBO, falling back to NON")
        return {"value": "NON"}

    # ── Accounts Management ──────────────────────────────────────────────────

    def _get_default_expense_account(self) -> dict:
        """
        Fetch the first available Expense account from QBO to use for line items.
        Caches it in memory for the lifecycle of the service.
        """
        if self.default_expense_account:
            return self.default_expense_account

        try:
            # specifically exclude SubAccounts to avoid API validation errors
            query = "SELECT * FROM Account WHERE AccountType = 'Expense' AND SubAccount = false MAXRESULTS 1"
            resp = self._request("GET", "query", params={"query": query})
            
            if resp.status_code == 200:
                accounts = resp.json().get("QueryResponse", {}).get("Account", [])
                if accounts:
                    acc = accounts[0]
                    self.default_expense_account = {
                        "value": str(acc.get("Id")),
                        "name": str(acc.get("Name"))
                    }
                    print(f"[QBO] Found default expense account: {self.default_expense_account}")
                    return self.default_expense_account
            
            print(f"[QBO] Warning: Could not find an expense account. Falling back to ID 1.")
            return {"value": "1", "name": "Uncategorized Expense"}
        except Exception as e:
            print(f"[QBO] _get_default_expense_account error: {e}")
            return {"value": "1", "name": "Uncategorized Expense"}

    def _get_expense_account_by_name(self, account_name: str) -> dict:
        """
        Search for an Expense account by name. Relies on fuzzy matching.
        Returns the QBO AccountRef dict if found, otherwise falls back to the default account.
        """
        if not account_name or not account_name.strip():
            return self._get_default_expense_account()
            
        name_clean = account_name.lower().strip()
        
        # Check cache
        if name_clean in self.gl_cache:
            return self.gl_cache[name_clean]

        try:
            # specifically exclude SubAccounts
            query = "SELECT * FROM Account WHERE AccountType = 'Expense' AND SubAccount = false MAXRESULTS 100"
            resp = self._request("GET", "query", params={"query": query})
            
            if resp.status_code == 200:
                accounts = resp.json().get("QueryResponse", {}).get("Account", [])
                
                best_account = None
                best_score = 0
                
                for acc in accounts:
                    display_name = acc.get("Name", "")
                    score = fuzz.ratio(name_clean, display_name.lower().strip())
                    partial_score = fuzz.partial_ratio(name_clean, display_name.lower().strip())
                    top_score = max(score, partial_score)
                    
                    if top_score > best_score:
                        best_score = top_score
                        best_account = acc
                
                # If we get a decent match, use it
                if best_score >= FUZZY_MATCH_THRESHOLD and best_account:
                    matched_ref = {
                        "value": str(best_account.get("Id")),
                        "name": str(best_account.get("Name"))
                    }
                    print(f"[QBO] GL Code '{account_name}' matched to QBO Account: '{matched_ref['name']}' (score={best_score})")
                    self.gl_cache[name_clean] = matched_ref
                    return matched_ref
                else:
                    print(f"[QBO] No GL Code match for '{account_name}' (best score={best_score}). Using fallback.")
            
        except Exception as e:
            print(f"[QBO] _get_expense_account_by_name error: {e}")
            
        # Fall back to default
        fallback = self._get_default_expense_account()
        # cache the fallback so we don't keep searching for it
        self.gl_cache[name_clean] = fallback
        return fallback

    # ── Vendor Management ────────────────────────────────────────────────────

    def _validate_vendor(self, vendor_id: str) -> Optional[Dict]:
        """
        Check that a vendor ID is still active in QBO.
        Returns the vendor dict (with CurrencyRef) if valid, or None if
        the vendor has been deleted / deactivated / doesn't exist.
        """
        try:
            resp = self._request("GET", f"vendor/{vendor_id}")
            if resp.status_code == 200:
                vendor = resp.json().get("Vendor", {})
                if vendor.get("Active", True):
                    return vendor
                print(f"[QBO] Vendor ID={vendor_id} exists but is inactive.")
                return None
            else:
                print(f"[QBO] Vendor ID={vendor_id} validation failed: {resp.status_code}")
                return None
        except Exception as e:
            print(f"[QBO] _validate_vendor error: {e}")
            return None

    @staticmethod
    def _vendor_currency(vendor: Optional[Dict]) -> str:
        """Extract currency code from a QBO Vendor dict, defaulting to USD."""
        if vendor and isinstance(vendor.get("CurrencyRef"), dict):
            return vendor["CurrencyRef"].get("value", "USD")
        return "USD"

    def find_vendor(self, name: str) -> Optional[dict]:
        """
        Search QBO for a vendor by name using fuzzy matching.
        Returns the best-matching vendor dict or None.
        """
        try:
            query = "SELECT * FROM Vendor WHERE Active = true MAXRESULTS 100"
            resp  = self._request("GET", "query", params={"query": query})

            if resp.status_code != 200:
                print(f"[QBO] Vendor query failed: {resp.status_code} — {resp.text[:200]}")
                return None

            vendors = resp.json().get("QueryResponse", {}).get("Vendor", [])

            best_vendor = None
            best_score  = 0
            name_clean  = name.lower().strip()

            for vendor in vendors:
                display_name  = vendor.get("DisplayName", "")
                score         = fuzz.ratio(name_clean, display_name.lower().strip())
                partial_score = fuzz.partial_ratio(name_clean, display_name.lower().strip())
                top           = max(score, partial_score)

                if top > best_score:
                    best_score  = top
                    best_vendor = vendor

            if best_score >= FUZZY_MATCH_THRESHOLD:
                print(f"[QBO] Vendor matched via API: '{best_vendor['DisplayName']}' (score={best_score})")
                self.vendor_cache[name_clean] = best_vendor.get("Id")
                self._save_vendor_cache()
                return best_vendor

            print(f"[QBO] No vendor match for '{name}' (best score={best_score})")
            return None

        except Exception as e:
            print(f"[QBO] find_vendor error: {e}")
            return None

    def create_vendor(self, name: str, currency_code: str = "USD") -> Optional[dict]:
        """Create a new vendor in QBO. Returns the created vendor dict or None."""
        try:
            payload = {
                "DisplayName":      name,
                "PrintOnCheckName": name,
                "CurrencyRef": {"value": currency_code}
            }
            resp = self._request("POST", "vendor", json=payload)

            if resp.status_code in (200, 201):
                vendor = resp.json().get("Vendor", {})
                vendor_id = vendor.get("Id")
                print(f"[QBO] Created vendor: '{vendor.get('DisplayName')}' (ID={vendor_id})")
                
                # Cache it
                name_clean = name.lower().strip()
                self.vendor_cache[name_clean] = vendor_id
                self._save_vendor_cache()
                
                return vendor
            else:
                print(f"[QBO] create_vendor failed: {resp.status_code} — {resp.text[:300]}")
                return None

        except Exception as e:
            print(f"[QBO] create_vendor error: {e}")
            return None

    def get_or_create_vendor(self, name: str, currency_code: str = "USD") -> Tuple[Optional[str], str]:
        """
        Find vendor by name (fuzzy). Create if not found.
        Returns (vendor_id, vendor_currency) — vendor_id is None on failure.
        """
        if not name or not name.strip():
            print("[QBO] Vendor name is empty — cannot create bill without vendor.")
            return None, currency_code

        name_clean = name.lower().strip()

        # ── Check local cache, but validate the ID is still alive in QBO ──
        if name_clean in self.vendor_cache:
            cached_id = self.vendor_cache[name_clean]
            print(f"[QBO] Vendor '{name}' found in local cache (ID={cached_id}) — validating...")
            vendor = self._validate_vendor(cached_id)
            if vendor:
                vcur = self._vendor_currency(vendor)
                print(f"[QBO] Cached vendor validated (ID={cached_id}, currency={vcur})")
                return cached_id, vcur
            # Stale cache entry — evict and fall through
            print(f"[QBO] Cached vendor ID={cached_id} is invalid/deleted — evicting from cache.")
            del self.vendor_cache[name_clean]
            self._save_vendor_cache()

        # ── Fuzzy-search QBO for the vendor ─────────────────────────────────
        vendor = self.find_vendor(name)
        if not vendor:
            print(f"[QBO] Creating new vendor: '{name}' with currency {currency_code}")
            vendor = self.create_vendor(name, currency_code=currency_code)

        if vendor:
            return vendor.get("Id"), self._vendor_currency(vendor)
        return None, currency_code

    # ── Bill Verification ────────────────────────────────────────────────────

    def check_duplicate_bill(self, vendor_id: str, total_amount: float, txn_date: str) -> bool:
        """
        Check QBO for an existing Bill with the exact vendor, amount, and date.
        Returns True if a duplicate is found.
        """
        try:
            # Construct a safe query
            # QBO query amount must be a string comparison for strict equality or we can just fetch and verify locally
            query = f"SELECT * FROM Bill WHERE VendorRef = '{vendor_id}' AND TxnDate = '{txn_date}' MAXRESULTS 50"
            resp = self._request("GET", "query", params={"query": query})

            if resp.status_code != 200:
                print(f"[QBO] Duplicate check query failed: {resp.status_code}")
                return False
                
            bills = resp.json().get("QueryResponse", {}).get("Bill", [])
            
            for bill in bills:
                bill_amount = float(bill.get("TotalAmt", 0.0))
                # Consider it a duplicate if amounts match closely (ignoring tiny float drifted differences)
                if abs(bill_amount - total_amount) < 0.01:
                    print(f"[QBO] Duplicate bill found in QBO: ID={bill.get('Id')} for Amount={total_amount}")
                    return True
                    
            return False
        except Exception as e:
            print(f"[QBO] check_duplicate_bill error: {e}")
            return False

    # ── Bill Posting ─────────────────────────────────────────────────────────

    def post_bill(self, invoice_data: dict, vendor_id: str, vendor_currency: str = "USD") -> Tuple[str, str]:
        """
        Post a Bill to QBO for the given vendor.
        Returns (status, bill_id) where status is 'posted' or 'failed'.
        """
        try:
            # ── Dates ─────────────────────────────────────────────
            raw_date = str(invoice_data.get("date", "") or "").strip()
            txn_date = raw_date if len(raw_date) >= 10 else date.today().isoformat()

            raw_due  = str(invoice_data.get("due_date", "") or "").strip()
            due_date = raw_due if len(raw_due) >= 10 else txn_date  # fall back to invoice date

            # ── Amounts ───────────────────────────────────────────
            total_amount = float(invoice_data.get("total_amount", 0.0) or 0.0)

            # ── Line Items ────────────────────────────────────────
            line_items = invoice_data.get("line_items", []) or []

            if not line_items:
                # Fallback: single line for the whole invoice
                line_items = [{
                    "description": invoice_data.get("description", "Invoice Items"),
                    "amount": total_amount,
                }]

            # ── Resolve GL Account (once, for all lines) ──────────
            # Priority: pre-resolved ref from GLClassifier > fuzzy name fallback
            gl_account_ref = invoice_data.get("gl_account_ref")
            if not gl_account_ref:
                gl_account_ref = self._get_expense_account_by_name(
                    invoice_data.get("gl_code_suggested", "")
                )
            print(f"[QBO] Using GL account: {gl_account_ref}")

            # ── Tax ───────────────────────────────────────────────
            vat_amount = float(invoice_data.get("vat_amount", 0.0) or 0.0)
            is_uae = invoice_data.get("is_uae_invoice", False)
            apply_global_tax = invoice_data.get("apply_global_tax", False)

            # Default tax code for lines without a specific qbo_tax_code
            default_tax_name = "0.0% Z"

            qbo_lines = []
            for i, item in enumerate(line_items, start=1):
                item_amount = float(item.get("amount", 0.0) or 0.0)
                if item_amount <= 0:
                    continue

                # Per-line tax code from vat_processor, or default
                line_tax_name = item.get("qbo_tax_code", default_tax_name)
                line_tax_ref = self._resolve_tax_code_by_name(line_tax_name)

                qbo_lines.append({
                    "Id":         str(i),
                    "Amount":     round(item_amount, 2),
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef":    gl_account_ref,
                        "BillableStatus": "NotBillable",
                        "TaxCodeRef":     line_tax_ref,
                    },
                    "Description": str(item.get("description", "") or ""),
                })

            # Safety: always have at least one line
            if not qbo_lines:
                fallback_tax_ref = self._resolve_tax_code_by_name(default_tax_name)
                qbo_lines = [{
                    "Id":         "1",
                    "Amount":     max(round(total_amount, 2), 0.01),
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef":    gl_account_ref,
                        "BillableStatus": "NotBillable",
                        "TaxCodeRef":     fallback_tax_ref,
                    },
                    "Description": "Invoice",
                }]

            # ── Currency ──────────────────────────────────────────
            # Always use the vendor's currency (QBO requires bill currency
            # to match the vendor's currency).  Log a warning if they differ.
            invoice_currency = str(invoice_data.get("currency", "USD") or "USD").upper()
            if invoice_currency == "CURRENCY_DEFAULTED_TO_USD":
                invoice_currency = "USD"

            currency_code = vendor_currency  # authoritative source
            if invoice_currency != currency_code:
                print(
                    f"[QBO] Currency mismatch: invoice says '{invoice_currency}' "
                    f"but vendor is '{currency_code}'. Using vendor currency."
                )

            # ── Build Payload ─────────────────────────────────────
            payload = {
                "VendorRef": {"value": vendor_id},
                "Line":      qbo_lines,
                "TxnDate":   txn_date,
                "DueDate":   due_date,
                "DocNumber": str(invoice_data.get("invoice_number", "") or "")[:21], # QBO trims at 21 chars
                "CurrencyRef": {
                    "value": currency_code
                },
                "PrivateNote": (
                    f"Auto-imported | "
                    f"File: {invoice_data.get('file_id', '')} | "
                    f"Supplier: {invoice_data.get('supplier_name', '')}"
                )[:4000],
            }

            # ── Tax detail on the payload (UAE invoices only) ─────
            if apply_global_tax and vat_amount > 0:
                payload["GlobalTaxCalculation"] = "TaxExcluded"
                payload["TxnTaxDetail"] = {
                    "TotalTax": round(vat_amount, 2),
                }
            
            # If not USD (which is likely the home currency), set ExchangeRate to 1.0 
            # so the API doesn't complain about an unknown exchange rate.
            if currency_code != "USD":
                payload["ExchangeRate"] = 1.0

            print(f"[QBO] Sending Bill payload: {json.dumps(payload, indent=2)}")

            resp = self._request("POST", "bill", json=payload)

            if resp.status_code in (200, 201):
                bill    = resp.json().get("Bill", {})
                bill_id = str(bill.get("Id", ""))
                print(f"[QBO] Success: Bill posted — ID: {bill_id}")
                return "posted", bill_id
            else:
                print(f"[QBO] post_bill failed: {resp.status_code} — {resp.text}")
                return "failed", ""

        except Exception as e:
            print(f"[QBO] post_bill error: {e}")
            return "failed", ""

    def attach_document(self, bill_id: str, file_path: str) -> bool:
        """
        Upload a file to QBO and attach it to the specific Bill ID.
        """
        if not os.path.exists(file_path):
            print(f"[QBO] Cannot attach document: file not found at {file_path}")
            return False
            
        try:
            filename = os.path.basename(file_path)
            # Find MIME type
            ext = filename.lower()
            if ext.endswith(".pdf"): mime_type = "application/pdf"
            elif ext.endswith(".png"): mime_type = "image/png"
            elif ext.endswith(".jpg") or ext.endswith(".jpeg"): mime_type = "image/jpeg"
            else: mime_type = "application/octet-stream"

            request_metadata = {
                "AttachableRef": [
                    {
                        "EntityRef": {
                            "type": "Bill",
                            "value": str(bill_id)
                        }
                    }
                ],
                "FileName": filename,
                "ContentType": mime_type
            }

            with open(file_path, "rb") as f:
                file_content = f.read()

            m = MultipartEncoder(
                fields={
                    'file_metadata_01': ('', json.dumps(request_metadata), 'application/json'),
                    'file_content_01': (filename, file_content, mime_type)
                }
            )

            url = f"{self.base_url}/v3/company/{self.realm_id}/upload"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": m.content_type,
                "Accept": "application/json"
            }

            resp = requests.post(url, headers=headers, data=m, timeout=45)
            
            # Auto-refresh on 401 Unauthorized
            if resp.status_code == 401:
                print("[QBO] 401 received on attachment — refreshing token and retrying...")
                if self._do_refresh():
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    resp = requests.post(url, headers=headers, data=m, timeout=45)
            
            if resp.status_code in (200, 201):
                print(f"[QBO] Success: Document attached to Bill {bill_id} successfully.")
                return True
            else:
                print(f"[QBO] Document attachment failed: {resp.status_code} — {resp.text[:400]}")
                return False

        except Exception as e:
            print(f"[QBO] attach_document error: {e}")
            return False

    # ── Public Entry Point ────────────────────────────────────────────────────

    def sync(self, invoice_data: dict, file_path: str = None) -> Tuple[str, str]:
        """
        Main entry point called by drive_processor and ocr_engine.

        Steps:
          1. Pre-posting validation
          2. Resolve vendor (find or create)
          3. Duplicate check
          4. Post Bill
          5. Attach document

        Returns:
          (qbo_status, qbo_bill_id)
          qbo_status is 'posted', 'failed', 'duplicate_skipped', or 'needs_review'
        """
        supplier = str(invoice_data.get("supplier_name", "") or "").strip()
        total_amount = float(invoice_data.get("total_amount", 0.0) or 0.0)
        
        raw_date = str(invoice_data.get("date", "") or "").strip()
        txn_date = raw_date if len(raw_date) >= 10 else date.today().isoformat()

        # 1. Pre-posting validation
        if not supplier or total_amount <= 0 or not raw_date:
            print("[QBO] Sync skipped: Validation failed (missing vendor, positive amount, or date). Needs Review.")
            return "needs_review", ""

        print(f"[QBO] sync() — vendor: '{supplier}' | Amount: {total_amount} | Date: {txn_date}")
        
        currency_code = str(invoice_data.get("currency", "USD") or "USD").upper()
        if currency_code == "CURRENCY_DEFAULTED_TO_USD":
            currency_code = "USD"

        # 2. Resolve vendor (now also returns the vendor's QBO currency)
        vendor_id, vendor_currency = self.get_or_create_vendor(supplier, currency_code=currency_code)
        if not vendor_id:
            print("[QBO] Could not resolve vendor — aborting bill post.")
            return "failed", ""

        # 3. Duplicate check
        if self.check_duplicate_bill(vendor_id, total_amount, txn_date):
            print("[QBO] Duplicate detected. Skipping post.")
            return "duplicate_skipped", ""

        # 4. Post Bill (use vendor_currency so CurrencyRef matches the vendor)
        status, bill_id = self.post_bill(invoice_data, vendor_id, vendor_currency=vendor_currency)
        
        # 5. Attach document (if bill succeeded and file provided)
        if status == "posted" and bill_id and file_path:
            self.attach_document(bill_id, file_path)

        return status, bill_id
