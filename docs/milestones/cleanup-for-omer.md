# Cleanup for Review

**Goal:** Strip everything not part of the live Railway production pipeline so the repo is clean for Omer's review.

---

## A. Files to DELETE

### Dead Tesseract / Legacy OCR Path
These modules are only reachable via the Tesseract fallback branch in `ocr_engine.py` (lines 160-227), which has never fired in production. Once that branch is removed, they become completely unreachable from `app.py`.

| File | Role | Imported by |
|------|------|-------------|
| `constants.py` | Hardcoded filename, tesseract path | `ocr_engine.py` (fallback), `main.py`, `handler.py`, `manual_extracter.py` |
| `converter.py` | PDF-to-JPEG via cv2 | `ocr_engine.py` (fallback), `main.py` |
| `preproces.py` | Binary image morphology via cv2 | `ocr_engine.py` (fallback), `main.py` |
| `run_ocr.py` | Tesseract OCR runner | `ocr_engine.py` (fallback), `main.py` |
| `extraction.py` | Parse Tesseract output.txt | `ocr_engine.py` (fallback), `main.py` |
| `tables.py` | Table detection via cv2 | `ocr_engine.py` (fallback), `main.py` |
| `handler.py` | cv2 coordinate picker GUI | `extraction.py` (conditional) |
| `manual_extracter.py` | cv2+pytesseract manual crop GUI | Not imported (standalone) |

### Standalone Desktop GUI
| File | Role |
|------|------|
| `main.py` | tkinter desktop app — imports all Tesseract modules, cv2, numpy, xlsxwriter. Completely disconnected from the FastAPI server. |

### Diagnostic Scripts (one-off debugging tools)
| File | Purpose |
|------|---------|
| `diag_ocr.py` | Test OCR extraction locally |
| `diag_post_bill.py` | Test QBO bill posting |
| `diag_post_bill_aed.py` | Test QBO bill posting (AED) |
| `diag_qbo.py` | Test QBO connection |
| `diag_qbo_accounts.py` | Dump QBO accounts |
| `diag_sheets.py` | Test Sheets service |

### Stale Text / Doc Files
| File | Content |
|------|---------|
| `APIs_Used.txt` | API notes (stale) |
| `debug_output.txt` | Debug log |
| `error_and_solution.txt` | Troubleshooting notes |
| `log.txt` | Log output |
| `log2.txt` | Log output |
| `log3.txt` | Log output |
| `log4.txt` | Log output |
| `main_output.txt` | Main script output |
| `main_output_2.txt` | Main script output |
| `main_output_3.txt` | Main script output |
| `newfile.txt` | "this is in testing branch" |
| `out.txt` | OCR output dump |
| `qbo_error.txt` | QBO error log |
| `test_out.txt` | Test output |

### Other Junk
| File | Reason |
|------|--------|
| `Procfile` | Unused — Railway uses `Dockerfile`, not Heroku-style Procfile |
| `__init__.py` | Root-level package marker not needed; `services/`, `utils/`, `workers/` have their own |
| `example/` | Sample PDFs for the legacy Tesseract pipeline (`Sample2.pdf`, `Sample8.pdf`, `sample3.pdf`) |
| `readme_images/` | Screenshots for the old README (`img1.jpg`, `img2.jpg`, `img3.jpg`) |

---

## B. Import Graph — Proof of Unreachability

Production entry point: **`app.py`**

```
app.py
  -> ocr_engine  (module-level import)
  -> workers.drive_processor  (lazy import in lifespan)
  -> services.quickbooks  (lazy import in callback)

ocr_engine.py (AFTER removing Tesseract fallback)
  -> os, pathlib, shutil, json, dotenv
  -> services.openai_extractor
  -> services.sheets_service
  -> services.quickbooks
  -> services.gl_classifier
  -> services.vat_processor
  -> utils.credentials_helper
  (NO references to: constants, converter, preproces, run_ocr,
   extraction, tables, handler, manual_extracter, cv2, numpy)

workers/drive_processor.py
  -> services.drive_watcher
  -> services.openai_extractor
  -> services.quickbooks
  -> services.sheets_service
  -> services.vat_processor
  -> services.gl_classifier
  -> utils.credentials_helper
```

After removing the fallback from `ocr_engine.py`, these modules have ZERO importers in the production tree:
`constants`, `converter`, `preproces`, `run_ocr`, `extraction`, `tables`, `handler`, `manual_extracter`, `main`

---

## C. Files to MODIFY

| File | Change |
|------|--------|
| `ocr_engine.py` | Remove Tesseract fallback (lines 4-10 imports + lines 160-227 fallback block). Remove `import cv2`, `import numpy as np`. Keep all GPT-4o / QBO / Sheets / GL logic intact. |
| `requirements.txt` | Remove: `pytesseract`, `opencv-python-headless>=4.2.0`, `numpy`, `xlsxwriter` |
| `Dockerfile` | Remove: `tesseract-ocr`, `tesseract-ocr-ara` from apt-get. Keep `poppler-utils` (needed by `pdf2image`). Remove `Details` from `mkdir`. |
| `README.md` | Full rewrite: describe GPT-4o pipeline, UAE VAT, QBO sync, Drive polling, Sheets logging, Railway deployment. |

---

## D. Name Search Results

```
$ git grep -ri 'anshum'  →  NOT FOUND
$ git grep -ri 'ishaan'  →  NOT FOUND
```

Neither name appears in any tracked file. No action needed. They may exist in git commit history, which we will NOT rewrite per the hard constraints.

---

## E. Smoke-Test Commands (after each commit)

```bash
python -c "import app"
python -c "import ocr_engine"
python -c "from workers.drive_processor import DriveProcessor"
```

All three must exit with code 0 (no ImportError). If any fails, the commit is reverted and reassessed.

---

## F. Commit Sequence

1. `refactor(ocr_engine): remove Tesseract fallback path`
2. `chore: delete unused legacy OCR modules`
3. `chore(deps): drop pytesseract, opencv-python-headless, numpy, xlsxwriter`
4. `chore(docker): remove tesseract apt packages`
5. `chore: delete diagnostic scripts and stale text files`
6. `docs: rewrite README for GPT-4o + QBO pipeline`

No commit 7 needed — no names found in tracked files.
