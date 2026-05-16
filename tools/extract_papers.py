import os
import sys
from pathlib import Path
from PyPDF2 import PdfReader
import re


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs_texts"
OUT_DIR.mkdir(exist_ok=True)

KEYWORDS = [
    "experiment",
    "experiments",
    "method",
    "methodology",
    "result",
    "results",
    "irrigation",
    "digital twin",
    "threshold",
    "water",
    "simulation",
]


def extract_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        return "\n".join(texts)
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")
        return ""


def keyword_sentences(text: str, keywords=KEYWORDS, max_sentences=10):
    # split into sentences roughly
    candidates = re.split(r'(?<=[.!?])\s+', text.replace('\n', ' '))
    out = []
    for s in candidates:
        lowered = s.lower()
        if any(k in lowered for k in keywords):
            out.append(s.strip())
        if len(out) >= max_sentences:
            break
    return out


def main():
    pdfs = list(ROOT.glob("*.pdf"))
    summary_path = OUT_DIR / "summaries.md"
    with summary_path.open("w", encoding="utf-8") as sumf:
        sumf.write("# Paper summaries\n\n")
        if not pdfs:
            sumf.write("No PDFs found in project root.\n")
            print("No PDFs found")
            return

        for pdf in pdfs:
            print(f"Processing {pdf.name}")
            text = extract_text(pdf)
            txt_out = OUT_DIR / (pdf.stem + ".txt")
            txt_out.write_text(text, encoding="utf-8")

            sumf.write(f"## {pdf.name}\n\n")
            # brief preview
            preview = text.strip()[:1200].replace('\n', ' ')
            if preview:
                sumf.write("**Preview:**\n\n```")
                sumf.write(preview)
                sumf.write("```\n\n")

            sentences = keyword_sentences(text, max_sentences=12)
            if sentences:
                sumf.write("**Keyword sentences / possible experiment descriptions:**\n\n")
                for s in sentences:
                    sumf.write(f"- {s}\n")
            else:
                sumf.write("No keyword-matching sentences found.\n")

            sumf.write("\n---\n\n")

    print(f"Extraction complete. Text and summaries written to {OUT_DIR}")


if __name__ == '__main__':
    main()
