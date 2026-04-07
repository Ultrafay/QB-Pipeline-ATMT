import os
from pathlib import Path
import shutil
import constants
# Import existing modules
import converter
import preproces
import run_ocr
import extraction
import tables
import cv2
import numpy as np
import json
from dotenv import load_dotenv

# Import New Services
from services.openai_extractor import OpenAIExtractor, InvoiceData
from services.sheets_service import GoogleSheetsService
from utils.credentials_helper import get_credentials_path

# QBO integration (optional)
try:
    from services.quickbooks import QuickBooksService
    _qbo_available = True
except ImportError:
    _qbo_available = False

# Load environment variables
load_dotenv()

# Initialize services
extractor = None
sheets = None
try:
    openai_key = os.getenv("OPENAI_API_KEY")

    if openai_key:
        extractor = OpenAIExtractor(
            api_key=openai_key,
            org_id=os.getenv("OPENAI_ORG_ID"),
            project_id=os.getenv("OPENAI_PROJECT_ID")
        )
        print("OpenAI Extractor (GPT-4o) initialized.")
    
    creds_path = get_credentials_path()
    sheets = GoogleSheetsService(
        credentials_path=creds_path,
        spreadsheet_id=os.getenv("GOOGLE_SHEET_ID")
    )
    print("Sheets service initialized.")
except Exception as e:
    print(f"Warning: Failed to initialize new services: {e}")
    extractor = None
    sheets = None

# GL Classifier — sheet-driven per-line GL categorisation
gl_classifier = None
try:
    from services.gl_classifier import GLClassifier
    _gl_sheet_id = os.getenv("GL_MAPPING_SHEET_ID", "")
    if sheets and _gl_sheet_id:
        gl_classifier = GLClassifier(sheets, _gl_sheet_id)
        gl_classifier.load_mapping()
        print("GL Classifier initialised — sheet-driven GL mapping active.")
    else:
        print("GL Classifier skipped: GL_MAPPING_SHEET_ID not set or Sheets not available.")
except Exception as _gl_err:
    print(f"GL Classifier not available: {_gl_err}")

# Initialize QBO (optional — skipped gracefully if not configured)
qbo = None
if _qbo_available and os.getenv("QBO_REALM_ID") and os.getenv("AUTO_PUSH_TO_QBO", "true").lower() == "true":
    try:
        qbo = QuickBooksService()
        print("QuickBooks service initialized successfully.")

        # Inject chart of accounts into GPT-4o prompt
        if extractor and qbo:
            try:
                account_names = qbo.get_all_account_names()
                if account_names:
                    extractor.set_chart_of_accounts(account_names)
            except Exception as _coa_err:
                print(f"Warning: Could not load chart of accounts: {_coa_err}")

        # Wire GL Classifier into QBO and run startup CoA validation
        if gl_classifier and qbo:
            qbo.gl_classifier = gl_classifier
            try:
                account_names = qbo.get_all_account_names()
                gl_classifier.validate_against_accounts(account_names)
            except Exception as _val_err:
                print(f"Warning: GL CoA validation failed: {_val_err}")

    except Exception as _qbo_err:
        print(f"Warning: QuickBooks init failed (continuing without QBO): {_qbo_err}")


def process_invoice(file_path: Path, file_id: str):
    """
    Orchestrates the OCR process using OpenAI GPT-4o with Tesseract fallback.
    """
    print(f"Processing {file_path} with ID {file_id}")
    filename = file_path.name
    
    # 1. Try AI Extraction (Primary)
    if os.getenv("USE_AI_EXTRACTION", "true").lower() == "true" and extractor:
        try:
            print(f"Attempting {type(extractor).__name__} Extraction...")
            if filename.lower().endswith(".pdf"):
                result_data = extractor.extract_from_pdf(str(file_path))
            else:
                result_data = extractor.extract_from_image(str(file_path))
            
            print("OpenAI Extraction Successful.")
            
            # Check for duplicates if enabled
            if sheets and os.getenv("DUPLICATE_CHECK_ENABLED", "true").lower() == "true":
                is_dup = sheets.check_duplicate(result_data.invoice_number, result_data.supplier_name)
                if is_dup:
                    print("Duplicate invoice detected.")
                    result_data.notes = (result_data.notes or "") + " [DUPLICATE DETECTED]"
            
            # Push to Sheets if enabled
            if sheets and os.getenv("AUTO_PUSH_TO_SHEETS", "true").lower() == "true":
                print("Pushing to Google Sheets...")
                sheets.append_invoice(result_data.dict(), file_id, filename)

            # Push to QuickBooks if enabled
            print(f"[DEBUG] QBO Enabled: {qbo is not None} | Invoice Data: {result_data.dict()}")
            if qbo:
                try:
                    invoice_dict = result_data.dict()
                    invoice_dict["file_id"] = file_id

                    # ── VAT Processing (per-line tax codes + foreign tax) ──
                    from services.vat_processor import process_vat
                    invoice_dict = process_vat(invoice_dict)
                    # ── End VAT Processing ─────────────────────────

                    print("Syncing to QuickBooks...")
                    qbo_status, qbo_bill_id = qbo.sync(invoice_dict, str(file_path))
                    if sheets:
                        sheets.update_qbo_status(file_id, qbo_status, qbo_bill_id)
                    result_data.notes = (result_data.notes or "") + f" [QBO:{qbo_status}]"
                    print(f"QuickBooks sync: {qbo_status} (bill_id={qbo_bill_id})")
                except Exception as _qbo_err:
                    print(f"QuickBooks sync error (non-fatal): {_qbo_err}")
                    if sheets:
                        sheets.update_qbo_status(file_id, "failed", "")
            
            return result_data.dict()
            
        except Exception as e:
            print(f"OpenAI extraction failed: {e}")
            if os.getenv("FALLBACK_TO_TESSERACT", "true").lower() != "true":
                raise e
            print("Falling back to Tesseract...")

    # 2. Fallback to Tesseract (Original Pipeline)
    # The original modules (preproces, run_ocr, extraction, tables) use
    # constants.filename to build hardcoded paths like Details/{filename}/Intermediates.
    # We override constants.filename to file_id so they write to Details/{file_id}/...
    
    original_filename = constants.filename
    constants.filename = file_id
    
    # Rebuild module-level path variables that were computed at import time
    cur_path = Path.cwd()
    details_path = cur_path / "Details" / file_id
    intermediates_path = details_path / "Intermediates"
    pages_dir = details_path / "Pages"
    rows_dir = details_path / "Rows"
    
    # Create directories
    details_path.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(exist_ok=True)
    intermediates_path.mkdir(exist_ok=True)
    rows_dir.mkdir(exist_ok=True)
    
    # Patch module-level path variables
    preproces.path_to_read = intermediates_path
    run_ocr.path_to_read = intermediates_path
    extraction.path_to_read = intermediates_path
    tables.path_to_read = intermediates_path
    tables.path_to_write = details_path
    
    try:
        # 1. Convert to JPEG
        if filename.lower().endswith(".pdf"):
            converter.convert_to_jpeg(file_path, pages_dir)
        else:
            img = cv2.imread(str(file_path))
            cv2.imwrite(str(pages_dir / "page1.jpg"), img)

        # 2. Preprocess (takes only img_path)
        page1_path = pages_dir / "page1.jpg"
        preproces.process(page1_path)
        
        # 3. Run OCR (takes no arguments)
        run_ocr.run_tesseract()
        
        # 4. Extraction (takes no arguments)
        buyer, seller, invoice = extraction.get_details()
        
        # 5. Tables (takes no arguments)
        table_data = tables.get_data()
        
        # Format results (Legacy Format)
        legacy_result = {
            "file_id": file_id,
            "buyer": buyer.tolist() if hasattr(buyer, "tolist") else buyer,
            "seller": seller.tolist() if hasattr(seller, "tolist") else seller,
            "invoice": invoice.tolist() if hasattr(invoice, "tolist") else invoice,
            "table": table_data.tolist() if hasattr(table_data, "tolist") else table_data,
            "extraction_method": "tesseract_fallback"
        }
        
        return legacy_result
    except Exception as e:
        import traceback
        print("!!!!!!!!!!!!!!!! OCR ENGINE CRASH !!!!!!!!!!!!!!!!")
        traceback.print_exc()
        raise e
    finally:
        # Restore original filename
        constants.filename = original_filename

