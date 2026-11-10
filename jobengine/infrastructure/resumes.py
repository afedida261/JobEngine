from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader


def read_resume(name: str, content: bytes) -> str:
    if len(content) > 5_000_000:
        raise ValueError("Resume files must be smaller than 5 MB.")
    extension = name.rsplit(".", 1)[-1].lower()
    if extension == "pdf":
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(p.extract_text() or "" for p in reader.pages[:15])
    elif extension == "docx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 5_000_000:
                raise ValueError("Resume document is too large after decompression.")
            root = ET.fromstring(archive.read(info))
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            text = "\n".join("".join(p.itertext()) for p in root.iter(ns + "p"))
    elif extension in {"txt", "md"}:
        text = content.decode("utf-8-sig")
    else:
        raise ValueError("Use a PDF, DOCX, TXT, or Markdown resume.")
    if len(text.strip()) < 30:
        raise ValueError(
            "No usable resume text was found. Paste your background instead; scanned PDFs need OCR."
        )
    if len(text) > 45000:
        raise ValueError("Resume text is too long; shorten it to 45,000 characters.")
    return text.strip()
