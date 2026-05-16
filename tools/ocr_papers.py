import fitz  # PyMuPDF
from pathlib import Path
from easyocr import Reader
import sys


ROOT = Path(__file__).resolve().parents[1]
PDFS = list(ROOT.glob("*.pdf"))
OUT_DIR = ROOT / "docs_texts"
OUT_DIR.mkdir(exist_ok=True)


def ocr_pdf(pdf_path: Path, reader: Reader) -> str:
    doc = fitz.open(pdf_path)
    texts = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        pix = page.get_pixmap(dpi=200)
        img_data = pix.tobytes("ppm")
        # EasyOCR can accept numpy arrays or image paths; use the bytes via temporary file is slower.
        try:
            result = reader.readtext(img_data, detail=0, paragraph=True)
            texts.append(" ".join(result))
        except Exception as e:
            print(f"OCR error on {pdf_path.name} page {page_num}: {e}")
    return "\n".join(texts)


def main():
    if not PDFS:
        print("No PDFs found in project root.")
        return

    reader = Reader(['en'], gpu=False)

    for pdf in PDFS:
        print(f"OCRing {pdf.name}")
        text = ocr_pdf(pdf, reader)
        out_file = OUT_DIR / (pdf.stem + "_ocr.txt")
        out_file.write_text(text, encoding='utf-8')
        print(f"Wrote OCR text to {out_file}")

    print("OCR completed.")


if __name__ == '__main__':
    main()
