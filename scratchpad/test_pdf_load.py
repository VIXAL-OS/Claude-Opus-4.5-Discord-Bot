# Offline validation for !load_text PDF support (2026-07-16).
# Run from repo root: PYTHONIOENCODING=utf-8 python scratchpad/test_pdf_load.py
#
# Exercises _extract_pdf_text against real PDFs generated in-process
# (matplotlib for text PDFs, pypdf's PdfWriter for encryption/blank pages)
# plus the wiring checks: import guard, _fetch_file_bytes, handler filter.

import io
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pypdf import PdfReader, PdfWriter

import bot

PASS = 0
FAIL = 0

def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_text_pdf(pages: list[str]) -> bytes:
    """Render each string as one PDF page of real (extractable) text."""
    buf = io.BytesIO()
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(buf) as pdf:
        for body in pages:
            fig = plt.figure(figsize=(8.5, 11))
            fig.text(0.1, 0.9, body, fontsize=12, va="top", wrap=True)
            pdf.savefig(fig)
            plt.close(fig)
    return buf.getvalue()


def encrypt_pdf(data: bytes, user_password: str, owner_password: str) -> bytes:
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password=owner_password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_blank_pdf(n_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


print("== wiring ==")
check("_HAS_PYPDF is True with pypdf installed", bot._HAS_PYPDF is True)
check("_extract_pdf_text exists", callable(getattr(bot, "_extract_pdf_text", None)))
# _fetch_file_bytes lives on the same class as _fetch_text_file
owner = None
for name in dir(bot):
    obj = getattr(bot, name)
    if isinstance(obj, type) and hasattr(obj, "_fetch_text_file"):
        owner = obj
        break
check("_fetch_file_bytes on the manager class",
      owner is not None and hasattr(owner, "_fetch_file_bytes"),
      f"owner={owner}")

print("== normal multi-page PDF ==")
pdf = make_text_pdf([
    "Chapter 1\n\nIt was a dark and stormy night in the neural net.",
    "Chapter 2\n\nThe gradient descended, slowly, into the valley.",
])
text, n_pages = bot._extract_pdf_text(pdf)
check("page count == 2", n_pages == 2, f"got {n_pages}")
check("chapter 1 text extracted", "dark and stormy" in text, text[:200])
check("chapter 2 text extracted", "gradient descended" in text)
check("heading lines survive for _detect_chapter_breaks",
      "Chapter 1" in text and "Chapter 2" in text)

print("== owner-password-only encryption (should open with empty user pw) ==")
enc_owner = encrypt_pdf(pdf, user_password="", owner_password="hunter2")
try:
    text2, n2 = bot._extract_pdf_text(enc_owner)
    check("owner-only encrypted PDF extracts", "dark and stormy" in text2 and n2 == 2)
except ValueError as e:
    check("owner-only encrypted PDF extracts", False, str(e))

print("== user-password-locked PDF (should raise a user-facing ValueError) ==")
enc_user = encrypt_pdf(pdf, user_password="sekrit", owner_password="hunter2")
try:
    bot._extract_pdf_text(enc_user)
    check("locked PDF raises ValueError", False, "no exception raised")
except ValueError as e:
    check("locked PDF raises ValueError", "password-protected" in str(e), str(e))
except Exception as e:
    check("locked PDF raises ValueError", False, f"wrong type: {type(e).__name__}: {e}")

print("== blank/image-only PDF (no extractable text) ==")
try:
    bot._extract_pdf_text(make_blank_pdf(3))
    check("blank PDF raises ValueError", False, "no exception raised")
except ValueError as e:
    check("blank PDF raises ValueError",
          "no extractable text" in str(e) and "OCR" in str(e), str(e))
except Exception as e:
    check("blank PDF raises ValueError", False, f"wrong type: {type(e).__name__}: {e}")

print("== garbage bytes (handler catches generic Exception) ==")
try:
    bot._extract_pdf_text(b"this is not a pdf at all" * 100)
    check("garbage raises", False, "no exception raised")
except Exception:
    check("garbage raises", True)

print("== handler accepts .pdf, title strip, help text ==")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py"),
           encoding="utf-8").read()
check('extension filter includes ".pdf"',
      '(".txt", ".html", ".htm", ".md", ".pdf")' in src)
check("title regex strips .pdf", r"\.(txt|html|htm|md|pdf)$" in src)
check("!help mentions .pdf for !load_text", ".txt/.html/.md/.pdf attachment" in src)
check("pip-install hint for missing pypdf present", "PDF files need `pypdf`" in src)
check("extraction runs off the event loop",
      "asyncio.to_thread(_extract_pdf_text, data)" in src)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
