"""
ocr_engine.py — Facade module for OCR, Google Sheets, and QuickBooks integrations.

Exposes:
  - process_invoice(file_path, file_id) -> dict
  - sheets  : GoogleSheetsClient instance, or None if not configured
  - qbo     : QuickBooksClient instance, or None if not configured

All integrations degrade gracefully when credentials are absent so the app
can start without any external services configured.
"""

from __future__ import annotations

import os
import json
import traceback
from pathlib import Path
from typing import Optional, Any


# ── process_invoice ───────────────────────────────────────────────────────────

def process_invoice(file_path, file_id: str) -> dict:
    """
    Run OCR extraction on an invoice file and return a JSON-serialisable dict.

    Tries the OpenAI GPT-4o extractor first (requires OPENAI_API_KEY).
    Falls back to a lightweight error payload if extraction fails so the
    endpoint always returns a structured response rather than a 500.
    """
    file_path = Path(file_path)
    ext = file_path.suffix.lower()

    # ── Primary path: OpenAI Vision extractor ────────────────────────────────
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    if openai_api_key:
        try:
            from services.openai_extractor import OpenAIExtractor

            org_id     = os.getenv("OPENAI_ORG_ID")
            project_id = os.getenv("OPENAI_PROJECT_ID")
            extractor  = OpenAIExtractor(
                api_key    = openai_api_key,
                org_id     = org_id or None,
                project_id = project_id or None,
            )

            if ext == ".pdf":
                invoice_data = extractor.extract_from_pdf(str(file_path))
            else:
                invoice_data = extractor.extract_from_image(str(file_path))

            result = invoice_data.model_dump()
            result["file_id"] = file_id
            result["source_file"] = file_path.name

            # Persist to Google Sheets if available
            if sheets:
                try:
                    sheets.append_invoice(file_id, result)
                except Exception as sheet_err:
                    print(f"[ocr_engine] Sheets append failed: {sheet_err}")

            return result

        except Exception as e:
            print(f"[ocr_engine] OpenAI extraction failed: {e}")
            traceback.print_exc()
            # Fall through to error payload

    # ── Fallback: return a structured error payload ───────────────────────────
    print("[ocr_engine] No OPENAI_API_KEY set or extraction failed — returning empty payload")
    return {
        "file_id": file_id,
        "source_file": file_path.name,
        "extraction_method": "none",
        "extraction_confidence": "low",
        "error": "OPENAI_API_KEY not configured or extraction failed",
        "date": None,
        "supplier_name": None,
        "invoice_number": None,
        "total_amount": None,
        "currency": "USD",
        "line_items": [],
    }


# ── GoogleSheetsClient ────────────────────────────────────────────────────────

class GoogleSheetsClient:
    """
    Thin wrapper around the Google Sheets API for invoice tracking.

    Expected sheet columns (1-indexed):
      A: file_id  B: supplier_name  C: invoice_number  D: date
      E: total_amount  F: currency  G: status  H: qb_transaction_id
    """

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, spreadsheet_id: str, credentials_json: str):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_data = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(creds_data, scopes=self.SCOPES)
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Invoices")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _range(self, cell_range: str) -> str:
        return f"{self._sheet_name}!{cell_range}"

    def _find_row(self, file_id: str) -> Optional[int]:
        """Return the 1-based row index for the given file_id, or None."""
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._range("A:A"))
            .execute()
        )
        rows = result.get("values", [])
        for idx, row in enumerate(rows, start=1):
            if row and row[0] == file_id:
                return idx
        return None

    # ── public API ────────────────────────────────────────────────────────────

    def append_invoice(self, file_id: str, data: dict) -> None:
        """Append a new invoice row to the sheet."""
        row = [
            file_id,
            data.get("supplier_name") or "",
            data.get("invoice_number") or "",
            data.get("date") or "",
            data.get("total_amount") or "",
            data.get("currency") or "USD",
            "Pending",
            "",  # qb_transaction_id
        ]
        self._service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=self._range("A:H"),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    def get_invoices(self, status_filter: Optional[str] = None) -> list:
        """Return all invoice rows, optionally filtered by status."""
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._range("A:H"))
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            return []

        # First row may be a header — skip it if it starts with "file_id" or "File"
        start = 1 if (rows[0] and str(rows[0][0]).lower() in ("file_id", "file id")) else 0
        invoices = []
        for row in rows[start:]:
            # Pad short rows
            while len(row) < 8:
                row.append("")
            invoice = {
                "file_id":          row[0],
                "supplier_name":    row[1],
                "invoice_number":   row[2],
                "date":             row[3],
                "total_amount":     row[4],
                "currency":         row[5],
                "status":           row[6],
                "qb_transaction_id": row[7],
            }
            if status_filter is None or invoice["status"] == status_filter:
                invoices.append(invoice)
        return invoices

    def update_status(
        self,
        file_id: str,
        status: str,
        qb_transaction_id: Optional[str] = None,
    ) -> bool:
        """Update the status (and optionally QB transaction ID) for a row."""
        row_idx = self._find_row(file_id)
        if row_idx is None:
            return False

        # Update status column (G)
        self._service.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet_id,
            range=self._range(f"G{row_idx}"),
            valueInputOption="RAW",
            body={"values": [[status]]},
        ).execute()

        if qb_transaction_id is not None:
            self._service.spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=self._range(f"H{row_idx}"),
                valueInputOption="RAW",
                body={"values": [[qb_transaction_id]]},
            ).execute()

        return True


# ── QuickBooksClient ──────────────────────────────────────────────────────────

class QuickBooksClient:
    """
    Minimal QuickBooks Online REST client.

    Reads credentials from environment variables:
      QBO_CLIENT_ID, QBO_CLIENT_SECRET,
      QBO_ACCESS_TOKEN, QBO_REFRESH_TOKEN, QBO_REALM_ID
    """

    _BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
    _SANDBOX_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company"

    def __init__(self):
        import requests as _requests  # local import to avoid shadowing at module level
        self._requests = _requests

        self.access_token  = os.getenv("QBO_ACCESS_TOKEN", "")
        self.refresh_token = os.getenv("QBO_REFRESH_TOKEN", "")
        self.realm_id      = os.getenv("QBO_REALM_ID", "")
        self.company       = os.getenv("QBO_COMPANY_NAME", "")
        self._sandbox      = os.getenv("QBO_SANDBOX", "false").lower() == "true"

    @property
    def _base(self) -> str:
        return self._SANDBOX_URL if self._sandbox else self._BASE_URL

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Make an authenticated request to the QBO API."""
        url = f"{self._base}/{self.realm_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        response = self._requests.request(
            method,
            url,
            headers=headers,
            timeout=30,
            **kwargs,
        )
        return response


# ── Module-level singletons ───────────────────────────────────────────────────

def _init_sheets() -> Optional[GoogleSheetsClient]:
    spreadsheet_id   = os.getenv("GOOGLE_SPREADSHEET_ID", "")
    credentials_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")

    if not spreadsheet_id or not credentials_json:
        print("[ocr_engine] Google Sheets not configured (GOOGLE_SPREADSHEET_ID / GOOGLE_SHEETS_CREDENTIALS_JSON missing)")
        return None

    try:
        client = GoogleSheetsClient(spreadsheet_id, credentials_json)
        print("[ocr_engine] Google Sheets client initialised")
        return client
    except Exception as e:
        print(f"[ocr_engine] Google Sheets init failed: {e}")
        traceback.print_exc()
        return None


def _init_qbo() -> Optional[QuickBooksClient]:
    if not os.getenv("QBO_CLIENT_ID") or not os.getenv("QBO_CLIENT_SECRET"):
        print("[ocr_engine] QuickBooks not configured (QBO_CLIENT_ID / QBO_CLIENT_SECRET missing)")
        return None

    try:
        client = QuickBooksClient()
        print("[ocr_engine] QuickBooks client initialised")
        return client
    except Exception as e:
        print(f"[ocr_engine] QuickBooks init failed: {e}")
        traceback.print_exc()
        return None


sheets: Optional[GoogleSheetsClient] = _init_sheets()
qbo:    Optional[QuickBooksClient]   = _init_qbo()
