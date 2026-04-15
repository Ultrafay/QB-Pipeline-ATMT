# Invoice OCR
This repo aims to convert scanned invoices to excel sheet using cv2 and pytesseract for reading the invoices. It converts the invoice to binary form and detects horizontal and vertical lines to construct the tabular data and perform tesseract reading on each block. It also generates an output txt file where all the extracted data are placed and named fields can be obtained by searching within it.
It supports both PDFs and Jpegs format.

## Dependencies
- OpenAI
- pdf2image [with poppler](https://pypi.org/project/pdf2image/)
- pytesseract (5.0 + ) [Recommended to install from here](https://github.com/tesseract-ocr/tesseract/wiki)


