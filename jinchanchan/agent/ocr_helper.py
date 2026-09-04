import easyocr
import numpy as np
import time
import re
import threading

_reader = None
_reader_ready = [False]

def _init_reader():
    global _reader
    print("[OCR] Initializing easyocr reader in background...")
    _reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
    _reader_ready[0] = True
    print("[OCR] Reader ready")

# 后台初始化
_bg_thread = threading.Thread(target=_init_reader, daemon=True)
_bg_thread.start()

def get_reader():
    while not _reader_ready[0]:
        time.sleep(0.5)
    return _reader


def ocr_text(image, detail=0, paragraph=True):
    reader = get_reader()
    results = reader.readtext(image, detail=detail, paragraph=paragraph)
    if isinstance(results, list):
        return " ".join(results).strip()
    return str(results).strip()


def read_round_number(image):
    text = ocr_text(image)
    match = re.search(r"(\d+)-(\d+)", text)
    if match:
        return match.group(0)
    return None


def read_countdown(image):
    text = ocr_text(image)
    match = re.search(r"(\d+):(\d+)", text)
    if match:
        return match.group(0)
    return None