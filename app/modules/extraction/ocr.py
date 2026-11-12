import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytesseract

from app.schemas.extraction import ExtractionMethod


@dataclass(slots=True)
class DocumentTextResult:
    pages_text: list[tuple[int, str]]
    method: ExtractionMethod
    ocr_confidence: float

    @property
    def full_text(self) -> str:
        return "\n\n".join(
            f"--- PÁGINA {page} ---\n{text}" for page, text in self.pages_text
        )


def _preprocess_for_ocr(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    # Escalado 2x si la resolución es baja para mejorar lectura de dígitos
    _h, w = gray.shape
    if w < 1600:
        scale = 2.0
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    denoised = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def _ocr_image(image_bgr: np.ndarray) -> tuple[str, float]:
    processed = _preprocess_for_ocr(image_bgr)

    # Detección de rotación en documentos escaneados con inclinación/giro
    try:
        osd = pytesseract.image_to_osd(processed, output_type=pytesseract.Output.DICT)
        angle = int(osd.get("rotate", 0))
        if angle == 90:
            processed = cv2.rotate(processed, cv2.ROTATE_90_CLOCKWISE)
        elif angle == 180:
            processed = cv2.rotate(processed, cv2.ROTATE_180)
        elif angle == 270:
            processed = cv2.rotate(processed, cv2.ROTATE_90_COUNTERCLOCKWISE)
    except pytesseract.TesseractError:
        pass

    config = "--oem 3 --psm 6"
    data = pytesseract.image_to_data(
        processed,
        lang="spa+eng",
        config=config,
        output_type=pytesseract.Output.DICT,
    )
    words: list[str] = []
    confs: list[float] = []
    for word, conf in zip(data["text"], data["conf"], strict=False):
        clean_word = word.strip()
        if clean_word:
            words.append(clean_word)
        try:
            conf_val = float(conf)
            if conf_val >= 0:
                confs.append(conf_val / 100.0)
        except (TypeError, ValueError):
            continue

    raw_text = pytesseract.image_to_string(processed, lang="spa+eng", config=config)
    avg_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
    return raw_text.strip(), avg_conf


def extract_document_text(file_path: Path) -> DocumentTextResult:
    """Lee texto nativo con PyMuPDF y cae a OpenCV+Tesseract si es escaneo o imagen."""
    suffix = file_path.suffix.lower()

    if suffix in {".png", ".jpg", ".jpeg"}:
        img = cv2.imread(str(file_path))
        if img is None:
            raise ValueError(f"No se pudo decodificar la imagen: {file_path}")
        text, conf = _ocr_image(img)
        return DocumentTextResult(
            pages_text=[(1, text)],
            method=ExtractionMethod.OCR,
            ocr_confidence=conf,
        )

    pages_text: list[tuple[int, str]] = []
    used_ocr = False
    ocr_confidences: list[float] = []

    with pymupdf.open(file_path) as doc:
        for idx, page in enumerate(doc, start=1):
            native_text = page.get_text("text").strip()
            alnum_chars = len(re.sub(r"\W", "", native_text))
            # Si la página tiene menos de 40 caracteres útiles, se trata como escaneo
            if alnum_chars >= 40:
                pages_text.append((idx, native_text))
            else:
                used_ocr = True
                pix = page.get_pixmap(dpi=250)
                img_array = np.frombuffer(pix.tobytes("png"), dtype=np.uint8)
                img_bgr = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                ocr_text, conf = _ocr_image(img_bgr)
                pages_text.append((idx, ocr_text))
                ocr_confidences.append(conf)

    if used_ocr:
        avg_conf = (
            round(sum(ocr_confidences) / len(ocr_confidences), 4)
            if ocr_confidences
            else 0.0
        )
        return DocumentTextResult(
            pages_text=pages_text,
            method=ExtractionMethod.OCR,
            ocr_confidence=avg_conf,
        )

    return DocumentTextResult(
        pages_text=pages_text,
        method=ExtractionMethod.DIGITAL_TEXT,
        ocr_confidence=0.99,
    )
