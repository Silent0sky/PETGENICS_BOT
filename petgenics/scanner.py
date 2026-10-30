from __future__ import annotations

import io
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image
import pytesseract
from pytesseract import Output
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from petgenics.models import Product


def _ocr(image: Image.Image) -> tuple[str, float]:
    if os.getenv("TESSERACT_CMD"):
        pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except (pytesseract.TesseractNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Tesseract OCR could not process the image: {exc}") from exc
    words: list[str] = []
    confidence: list[float] = []
    for text, raw_confidence in zip(data["text"], data["conf"]):
        text = text.strip()
        try:
            score = float(raw_confidence)
        except (ValueError, TypeError):
            continue
        if text and score >= 0:
            words.append(text)
            confidence.append(score / 100)
    return " ".join(words), (sum(confidence) / len(confidence) if confidence else 0.0)


def extract_document(content: bytes, filename: str) -> dict[str, Any]:
    suffix = Path(filename).suffix.lower()
    pages: list[dict[str, Any]] = []
    if suffix == ".pdf":
        try:
            document = fitz.open(stream=content, filetype="pdf")
        except (fitz.FileDataError, RuntimeError) as exc:
            raise ValueError(f"Could not open the PDF: {exc}") from exc
        for index, page in enumerate(document):
            text = page.get_text("text").strip()
            score = 1.0 if text else 0.0
            if not text:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                text, score = _ocr(image)
            pages.append({"page": index + 1, "text": text, "confidence": round(score, 3)})
    elif suffix in {".jpg", ".jpeg", ".png"}:
        try:
            image = Image.open(io.BytesIO(content)).convert("RGB")
        except (OSError, Image.UnidentifiedImageError) as exc:
            raise ValueError(f"Could not open the image: {exc}") from exc
        text, score = _ocr(image)
        pages.append({"page": 1, "text": text, "confidence": round(score, 3)})
    else:
        raise ValueError("Scanner accepts PDF, JPG and PNG files.")
    unreadable = [page["page"] for page in pages if not page["text"].strip()]
    full_text = "\n".join(page["text"] for page in pages)
    return {"pages": pages, "unreadable_pages": unreadable, "lines": match_lines(full_text, [])}


def match_lines(text: str, products: list[Product]) -> list[dict[str, Any]]:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        quantity_match = re.search(r"\b(?:qty|quantity)\s*[:=]?\s*(\d+(?:\.\d+)?)", line, re.I)
        code_match = None
        product_match = None
        for product in products:
            if re.search(
                rf"(?<![\w]){re.escape(product.product_code)}(?![\w])", line, re.IGNORECASE
            ):
                code_match = product
                break
        if code_match is None:
            normalized = " ".join(line.casefold().split())
            exact = [p for p in products if " ".join(p.name.casefold().split()) in normalized]
            if len(exact) == 1:
                product_match = exact[0]
        matched = code_match or product_match
        lines.append({
            "source_text": line,
            "product_code": matched.product_code if matched else None,
            "product_id": matched.id if matched else None,
            "product_name": matched.name if matched else None,
            "quantity": quantity_match.group(1) if quantity_match else None,
            "free_quantity": None,
            "rate": None,
            "discount_percent": None,
            "confidence": 0.95 if code_match else (0.80 if product_match else 0.0),
            "warning": (
                "Name-only product match is uncertain; verify manually."
                if product_match
                else (None if code_match and quantity_match else "Confirm product and quantity; no uncertain value was inferred.")
            ),
        })
    return lines


async def analyze_document(
    session: AsyncSession,
    content: bytes,
    filename: str,
    allowed_brands: set[str] | None = None,
) -> dict[str, Any]:
    extracted = extract_document(content, filename)
    query = select(Product).where(Product.active.is_(True))
    if allowed_brands is not None:
        query = query.where(Product.brand.in_(allowed_brands))
    products = list((await session.scalars(query)).all())
    combined_text = "\n".join(page["text"] for page in extracted["pages"])
    extracted["lines"] = match_lines(combined_text, products)
    extracted["requires_confirmation"] = True
    return extracted
