"""
Background Drive processor.
Polls a Google Drive folder for new invoices, processes them via OpenAI,
and pushes results to Google Sheets.
"""
import asyncio
import os
import uuid
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv
load_dotenv()

from services.drive_watcher import GoogleDriveWatcher
from services.openai_extractor import OpenAIExtractor
from services.sheets_service import GoogleSheetsService
from utils.credentials_helper import get_credentials_path

# QBO integration (imported lazily so missing deps don't break the rest)
try:
    from services.quickbooks import QuickBooksService
    _qbo_available = True
except ImportError:
    _qbo_available = False


class DriveProcessor:
    def __init__(self):
        self.poll_interval = int(os.getenv("DRIVE_POLL_INTERVAL", "10"))
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._processed_ids: Set[str] = set()
        self._stats = {
            "started_at": None,
            "last_poll": None,
            "files_processed": 0,
            "files_failed": 0,
        }

        # Initialize services
        creds_path = get_credentials_path()
        folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
        
        self.drive = GoogleDriveWatcher(
            credentials_path=creds_path,
            folder_id=folder_id
        )
        self.extractor = OpenAIExtractor(
            api_key=os.getenv("OPENAI_API_KEY"),
            org_id=os.getenv("OPENAI_ORG_ID"),
            project_id=os.getenv("OPENAI_PROJECT_ID")
        )
        self.sheets = GoogleSheetsService(
            credentials_path=creds_path,
            spreadsheet_id=os.getenv("GOOGLE_SHEET_ID")
        )

        # GL Classifier — sheet-driven per-line classification
        self.gl_classifier = None
        _gl_sheet_id = os.getenv("GL_MAPPING_SHEET_ID", "")
        if _gl_sheet_id:
            try:
                from services.gl_classifier import GLClassifier
                self.gl_classifier = GLClassifier(self.sheets, _gl_sheet_id)
                self.gl_classifier.load_mapping()
                print("[DriveProcessor] GL Classifier initialised.")
            except Exception as _gl_err:
                print(f"[DriveProcessor] GL Classifier init failed: {_gl_err}")
        else:
            print("[DriveProcessor] GL_MAPPING_SHEET_ID not set — GL Classifier disabled.")

        self.folder_id = folder_id

        # Initialize QuickBooks (optional — only if credentials are configured)
        self.qbo = None
        if _qbo_available and os.getenv("QBO_REALM_ID") and os.getenv("AUTO_PUSH_TO_QBO", "true").lower() == "true":
            try:
                self.qbo = QuickBooksService()
                print("[DriveProcessor] QuickBooks integration enabled.")
            except Exception as qbo_err:
                print(f"[DriveProcessor] QuickBooks init failed (continuing without QBO): {qbo_err}")
        else:
            print(f"[DriveProcessor] QBO Skipped -> available:{_qbo_available}, realm:{os.getenv('QBO_REALM_ID')}, auto_push:{os.getenv('AUTO_PUSH_TO_QBO')}")

        # Inject chart of accounts into GPT-4o prompt
        if self.qbo and self.extractor:
            try:
                account_names = self.qbo.get_all_account_names()
                if account_names:
                    self.extractor.set_chart_of_accounts(account_names)
            except Exception as _coa_err:
                print(f"[DriveProcessor] Could not load chart of accounts: {_coa_err}")

        # Wire GL Classifier into QBO + run startup CoA validation
        if self.gl_classifier and self.qbo:
            self.qbo.gl_classifier = self.gl_classifier
            try:
                account_names = self.qbo.get_all_account_names()
                self.gl_classifier.validate_against_accounts(account_names)
            except Exception as _val_err:
                print(f"[DriveProcessor] GL CoA validation failed: {_val_err}")

        print(f"[DriveProcessor] Initialized. Watching folder: {folder_id}")

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self):
        """Start the background polling loop."""
        if self.is_running:
            return
        self.is_running = True
        self._stats["started_at"] = datetime.now().isoformat()
        self._task = asyncio.create_task(self._poll_loop())
        print(f"[DriveProcessor] Started polling every {self.poll_interval}s")

    async def stop(self):
        """Stop the background polling loop."""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        print("[DriveProcessor] Stopped")

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "folder_id": self.folder_id,
            "poll_interval_seconds": self.poll_interval,
            "tracked_file_count": len(self._processed_ids),
            **self._stats
        }

    # ── Core Loop ───────────────────────────────────────────────

    async def _poll_loop(self):
        """Main polling loop. Runs in background."""
        while self.is_running:
            try:
                await self._poll_once()
            except Exception as e:
                print(f"[DriveProcessor] Poll error: {e}")
                traceback.print_exc()
            
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self):
        """Single poll iteration: list files → process new ones."""
        self._stats["last_poll"] = datetime.now().isoformat()
        
        # Run Drive API call in thread pool (it's blocking)
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(None, self.drive.list_new_files)
        
        if not files:
            return

        new_files = [f for f in files if f['id'] not in self._processed_ids]
        if not new_files:
            return

        print(f"[DriveProcessor] Found {len(new_files)} new file(s)")

        for file_info in new_files:
            await loop.run_in_executor(
                None, self._process_file, file_info
            )

    # ── File Processing ─────────────────────────────────────────

    def _process_file(self, file_info: dict):
        """Download, extract, push to Sheets, move file."""
        file_id = file_info['id']
        file_name = file_info['name']
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"[DriveProcessor] [{timestamp}] Processing: {file_name}")

        # Create temp file for download
        suffix = Path(file_name).suffix or ".tmp"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="invoice_")
        os.close(tmp_fd)

        try:
            # 1. Download
            self.drive.download_file(file_id, tmp_path)
            print(f"[DriveProcessor]   Downloaded to {tmp_path}")

            # 2. Extract via OpenAI
            if file_name.lower().endswith(".pdf"):
                result = self.extractor.extract_from_pdf(tmp_path)
            else:
                result = self.extractor.extract_from_image(tmp_path)
            
            print(f"[DriveProcessor]   Extracted: {result.supplier_name} / {result.invoice_number}")

            # 3. Check duplicates
            if os.getenv("DUPLICATE_CHECK_ENABLED", "true").lower() == "true":
                is_dup = self.sheets.check_duplicate(
                    result.invoice_number, result.supplier_name
                )
                if is_dup:
                    result.notes = (result.notes or "") + " [DUPLICATE DETECTED]"
                    print(f"[DriveProcessor]   ⚠ Duplicate detected")

            # 4. Push to Sheets
            internal_id = str(uuid.uuid4())
            self.sheets.append_invoice(result.dict(), internal_id, file_name)
            print(f"[DriveProcessor]   Pushed to Google Sheets")

            # 5. Post to QuickBooks
            print(f"[DEBUG] QBO Enabled: {self.qbo is not None} | Invoice Data: {result.dict()}")
            if self.qbo:
                try:
                    invoice_dict = result.dict()
                    invoice_dict["file_id"] = internal_id  # pass through for PrivateNote

                    # ── VAT Processing (per-line tax codes + foreign tax) ──
                    from services.vat_processor import process_vat
                    invoice_dict = process_vat(invoice_dict)
                    # ── End VAT Processing ───────────────────────────

                    qbo_status, qbo_bill_id = self.qbo.sync(invoice_dict, tmp_path)
                    self.sheets.update_qbo_status(internal_id, qbo_status, qbo_bill_id)
                    print(f"[DriveProcessor]   QBO: {qbo_status} (bill_id={qbo_bill_id})")
                except Exception as qbo_err:
                    print(f"[DriveProcessor]   QBO sync error (non-fatal): {qbo_err}")
                    self.sheets.update_qbo_status(internal_id, "failed", "")

            # 6. Move to Processed
            self.drive.move_to_processed(file_id)
            print(f"[DriveProcessor]   ✓ Moved to Processed")

            self._stats["files_processed"] += 1

        except Exception as e:
            print(f"[DriveProcessor]   ✗ FAILED: {e}")
            traceback.print_exc()
            
            # Move to Failed folder
            try:
                self.drive.move_to_failed(file_id)
                print(f"[DriveProcessor]   Moved to Failed folder")
            except Exception as move_err:
                print(f"[DriveProcessor]   Could not move to Failed: {move_err}")
            
            self._stats["files_failed"] += 1

        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            
            # Track this file ID regardless of outcome
            self._processed_ids.add(file_id)
