from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime
from typing import List, Dict, Any
import os

class GoogleSheetsService:
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    
    # Column headers for the invoice tracking sheet
    HEADERS = [
        "Timestamp",
        "File ID",
        "Line #", # New: Unique ID for each line (e.g. INV-123-L1)
        "File Name",
        "Invoice Date",
        "Supplier Name",
        "Supplier TRN",
        "Invoice Number",
        "Item Description", # Changed from generic Description
        "Quantity",      # New
        "Unit Price",    # New
        "Line Amount",   # New
        "Due Date",
        "Credit Terms",
        "Bill To",
        "GL Code (Suggested)",
        "Exclusive Amount",
        "VAT Amount",
        "Invoice Total", # Changed from Total Amount
        "Currency",
        "Confidence",
        "Status",
        "QB Transaction ID",
        "Notes",
        "QBO Status",    # posted / failed / duplicate / needs_review
        "QBO Bill ID",   # QuickBooks Bill ID
        "QBO Currency",  # New: currency or currency_defaulted_to_usd
    ]
    
    def __init__(self, credentials_path: str, spreadsheet_id: str):
        """Initialize with service account credentials"""
        if not os.path.exists(credentials_path):
             raise FileNotFoundError(f"Credentials file not found at {credentials_path}")
             
        self.spreadsheet_id = spreadsheet_id
        self.creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=self.SCOPES
        )
        self.service = build('sheets', 'v4', credentials=self.creds)
        self.sheet = self.service.spreadsheets()
    
    def ensure_headers(self, sheet_name: str = "Invoices"):
        """Create headers if sheet is empty"""
        try:
            # Check first row
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{sheet_name}!A1:X1"
            ).execute()
            values = result.get('values', [])
            
            if not values:
                # Append headers
                body = {
                    'values': [self.HEADERS]
                }
                self.sheet.values().append(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{sheet_name}!A1",
                    valueInputOption="RAW",
                    body=body
                ).execute()
                print("Headers added to Google Sheet.")
        except Exception as e:
            print(f"Error ensuring headers: {e}")

    
    def append_invoice(self, invoice_data: dict, file_id: str, filename: str) -> bool:
        """Append extracted invoice data as new row(s). One row per line item."""
        try:
            self.ensure_headers() # Simple check
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows_to_add = []

            # Check if we have line items
            line_items = invoice_data.get('line_items', [])
            
            # Fallback for old/empty data: create 1 row with generic description
            if not line_items:
                line_items = [{
                    "description": invoice_data.get('description', ''),
                    "quantity": 1,
                    "unit_price": invoice_data.get('total_amount', 0.0),
                    "amount": invoice_data.get('total_amount', 0.0)
                }]

            for index, item in enumerate(line_items, start=1):
                # specific line item fields
                line_id = f"{invoice_data.get('invoice_number', 'UNK')}-L{index}"
                
                # construct row matching HEADERS order
                row = [
                    timestamp,                                  # Timestamp
                    file_id,                                    # File ID
                    line_id,                                    # Line #
                    filename,                                   # File Name
                    invoice_data.get('date', ''),               # Invoice Date
                    invoice_data.get('supplier_name', ''),      # Supplier Name
                    invoice_data.get('supplier_trn', ''),       # Supplier TRN
                    invoice_data.get('invoice_number', ''),     # Invoice Number
                    item.get('description', ''),                # Item Description
                    item.get('quantity', 0),                    # Quantity
                    item.get('unit_price', 0),                  # Unit Price
                    item.get('amount', 0),                      # Line Amount
                    invoice_data.get('due_date', ''),           # Due Date
                    invoice_data.get('credit_terms', ''),       # Credit Terms
                    invoice_data.get('bill_to', ''),            # Bill To
                    invoice_data.get('gl_code_suggested', ''),  # GL Code
                    invoice_data.get('exclusive_amount', 0.0),  # Exclusive Amount
                    invoice_data.get('vat_amount', 0.0),        # VAT Amount
                    invoice_data.get('total_amount', 0.0),      # Invoice Total
                    invoice_data.get('currency', 'AED'),        # Currency
                    invoice_data.get('extraction_confidence', 'medium'), # Confidence
                    "Pending Review",                           # Status
                    "",                                         # QB ID
                    invoice_data.get('notes', ''),              # Notes
                    "",                                         # QBO Status (updated later)
                    "",                                         # QBO Bill ID (updated later)
                    invoice_data.get('currency', 'USD'),        # QBO Currency
                ]
                rows_to_add.append(row)

            body = {
                'values': rows_to_add
            }
            
            self.sheet.values().append(
                spreadsheetId=self.spreadsheet_id,
                range="Invoices!A:A", # Append to the end
                valueInputOption="USER_ENTERED",
                body=body
            ).execute()
            
            return True
            
        except Exception as e:
            print(f"Error appending to Google Sheet: {e}")
            return False
    
    def _find_row_by_file_id(self, file_id: str) -> int:
        """Helper to find the FIRST row number by File ID (Column B)"""
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id, range="Invoices!B:B"
            ).execute()
            values = result.get('values', [])

            for index, row in enumerate(values):
                if row and row[0] == file_id:
                    return index + 1  # Sheets are 1-indexed
            return -1
        except Exception as e:
            print(f"Error finding row: {e}")
            return -1

    def _find_all_rows_by_file_id(self, file_id: str) -> list:
        """Return ALL row numbers (1-indexed) matching a File ID (multi-line invoices)."""
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id, range="Invoices!B:B"
            ).execute()
            values = result.get('values', [])

            return [
                index + 1
                for index, row in enumerate(values)
                if row and row[0] == file_id
            ]
        except Exception as e:
            print(f"Error finding rows: {e}")
            return []

    def update_status(self, file_id: str, status: str, qb_transaction_id: str = None):
        """Update status column for a specific invoice"""
        row_num = self._find_row_by_file_id(file_id)
        if row_num == -1:
            print(f"File ID {file_id} not found in sheet.")
            return False
            
        try:
            # Status is Column R (18th column)
            range_name = f"Invoices!R{row_num}"
            body = {'values': [[status]]}
            self.sheet.values().update(
                spreadsheetId=self.spreadsheet_id, range=range_name,
                valueInputOption="RAW", body=body
            ).execute()
            
            if qb_transaction_id:
                # QB ID is Column S (19th column)
                range_id = f"Invoices!S{row_num}"
                body_id = {'values': [[qb_transaction_id]]}
                self.sheet.values().update(
                    spreadsheetId=self.spreadsheet_id, range=range_id,
                    valueInputOption="RAW", body=body_id
                ).execute()
                
            return True
        except Exception as e:
            print(f"Error updating status: {e}")
            return False

    def update_qbo_status(self, file_id: str, qbo_status: str, qbo_bill_id: str) -> bool:
        """
        Write QBO Status (col Y) and QBO Bill ID (col Z) for all rows
        belonging to this file_id (handles multi-line invoices).
        """
        row_nums = self._find_all_rows_by_file_id(file_id)
        if not row_nums:
            print(f"[Sheets] File ID '{file_id}' not found — cannot update QBO status.")
            return False

        try:
            for row_num in row_nums:
                # Column Y = QBO Status, Column Z = QBO Bill ID
                body_status = {'values': [[qbo_status]]}
                self.sheet.values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Invoices!Y{row_num}",
                    valueInputOption="RAW",
                    body=body_status,
                ).execute()

                body_bill = {'values': [[qbo_bill_id]]}
                self.sheet.values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"Invoices!Z{row_num}",
                    valueInputOption="RAW",
                    body=body_bill,
                ).execute()

            print(f"[Sheets] QBO status updated for {len(row_nums)} row(s): {qbo_status}")
            return True
        except Exception as e:
            print(f"[Sheets] Error updating QBO status: {e}")
            return False
    
    def get_invoices(self, status_filter: str = None) -> List[Dict]:
        """Get list of invoices, optionally filtered by status"""
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id, range="Invoices!A:AA"
            ).execute()
            values = result.get('values', [])
            
            if not values or len(values) < 2:
                return []
                
            headers = [h.lower().replace(" ", "_") for h in values[0]]
            invoices = []
            
            for row in values[1:]:
                # Map row list to dict using headers
                invoice = {}
                for i, value in enumerate(row):
                    if i < len(headers):
                        invoice[headers[i]] = value
                
                # Check filter
                current_status = invoice.get('status', '').lower()
                if status_filter and status_filter.lower() != current_status:
                    continue
                    
                invoices.append(invoice)
                
            return invoices
        except Exception as e:
            print(f"Error fetching invoices: {e}")
            return []

    def check_duplicate(self, invoice_number: str, supplier_name: str) -> bool:
        """Check if invoice already exists in sheet"""
        try:
             # Read columns E (Supplier) and G (Invoice No)
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id, range="Invoices!E:G"
            ).execute()
            values = result.get('values', [])
            
            if not values:
                return False
                
            for row in values:
                # Row format roughly: [Supplier, TRN, InvoiceNo] depends on range E:G
                # E=0, F=1, G=2
                if len(row) >= 3:
                    existing_supplier = row[0]
                    existing_invoice = row[2]
                    
                    if (str(invoice_number).strip().lower() == str(existing_invoice).strip().lower() and 
                        str(supplier_name).strip().lower() in str(existing_supplier).strip().lower()):
                        return True
            return False
            
        except Exception as e:
            print(f"Error checking duplicate: {e}")
            return False
