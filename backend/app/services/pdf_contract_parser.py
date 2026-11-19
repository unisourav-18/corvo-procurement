import io
import json
import re
import pdfplumber
from google import genai
from google.genai import types
from app.config import settings


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def parse_contract_pdf(pdf_bytes: bytes) -> dict:
    """
    Extracts structured contract header + Schedule A lines from a signed PDF
    using deterministic regex first, falling back to gemini-3.8-flash for novel layouts.
    """
    text = extract_text_from_pdf(pdf_bytes)

    cid_m = re.search(r"Contract\s+No\.?\s*([A-Z0-9-]+)", text, re.IGNORECASE)
    sup_m = re.search(
        r"and\s+(.+?),\s*[A-Za-z\s]+\(Supplier\s+ID\s+([A-Z0-9]+)\)",
        text,
        re.IGNORECASE,
    )
    term_m = re.search(
        r"valid\s+from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        text,
        re.IGNORECASE,
    )
    cap_m = re.search(
        r"not\s+exceed\s+INR\s+([\d,]+(?:\.\d+)?)",
        text,
        re.IGNORECASE,
    )

    line_pattern = re.compile(
        r"^\s*\d+\s+(ITM-\d+)\s+(.+?)\s+([A-Z]{2,6})\s+([\d,]+\.\d{2})\s*$",
        re.MULTILINE,
    )
    lines = []
    for m in line_pattern.finditer(text):
        lines.append({
            "item_code": m.group(1).strip(),
            "description": m.group(2).strip(),
            "uom": m.group(3).strip(),
            "rate": float(m.group(4).replace(",", "")),
        })

    if cid_m and sup_m and term_m and cap_m and lines:
        return {
            "contract_id": cid_m.group(1).strip(),
            "supplier_name": sup_m.group(1).strip(),
            "supplier_id": sup_m.group(2).strip(),
            "start_date": term_m.group(1).strip(),
            "end_date": term_m.group(2).strip(),
            "value_cap": float(cap_m.group(1).replace(",", "")),
            "consumed_value": 0.0,
            "lines": lines,
            "raw_text": text,
        }

    if settings.gemini_api_key:
        client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=15_000),
        )
        prompt = (
            "Extract the rate contract details from this text into JSON with keys: "
            "contract_id, supplier_id, supplier_name, start_date (YYYY-MM-DD), "
            "end_date (YYYY-MM-DD), value_cap (number), lines (list of {item_code, description, uom, rate}).\n\n"
            f"{text}"
        )
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = json.loads(resp.text)
        data.setdefault("consumed_value", 0.0)
        data["raw_text"] = text
        return data

    raise ValueError("Could not parse contract PDF structure and no GEMINI_API_KEY configured for fallback.")