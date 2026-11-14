from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


LATENCY_RE = re.compile(r"(?<!\d)(\d{1,4})\s*m\s*s\b", re.IGNORECASE)
RESOLUTION_RE = re.compile(r"(?<!\d)(2160|1080|720|540|480)\s*[pP]?\b")
LOADING_WORDS = ("加载", "缓冲", "连接中", "loading", "buffering", "connecting")
MAX_PLAUSIBLE_LATENCY_MS = 2000


@dataclass(frozen=True)
class Perception:
    texts: tuple[str, ...] = ()
    latency_ms: Optional[int] = None
    resolution: Optional[int] = None
    loading: bool = False
    ocr_confidence: float = 0.0
    vlm_state: Optional[str] = None


class OCRPerception:
    def __init__(self, app_name: Optional[str] = None, scene: Optional[str] = None):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError("缺少 rapidocr-onnxruntime，请先安装 requirements.txt") from exc
        self._engine = RapidOCR()
        self._latency_roi: Optional[tuple[float, float, float, float]] = None
        self.tracked_only = False
        self.app_name = (app_name or "").strip().lower()
        self.scene = (scene or "").strip().lower()
        if self.app_name == "honorofkings":
            # In-game network latency sits in the top-right status bar.
            self._latency_roi = (0.775, 0.005, 0.855, 0.045)
            self.tracked_only = True

    @property
    def has_latency_roi(self) -> bool:
        return self._latency_roi is not None

    def analyze(self, image: np.ndarray) -> Perception:
        if self._latency_roi is not None:
            height, width = image.shape[:2]
            x1, y1, x2, y2 = self._latency_roi
            crop = image[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
            if crop.size:
                target_height = 96
                scale = target_height / max(1, crop.shape[0])
                crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), target_height))
                results, _ = self._engine(crop, use_det=False, use_cls=False)
                for item in results or []:
                    if not isinstance(item, (list, tuple)) or not item:
                        continue
                    if isinstance(item[0], str):
                        text = str(item[0]).strip()
                        confidence = float(item[1]) if len(item) > 1 else 0.0
                    elif len(item) >= 3:
                        text = str(item[1]).strip()
                        confidence = float(item[2])
                    else:
                        continue
                    match = LATENCY_RE.search(text.replace("毫秒", "ms"))
                    if match:
                        value = int(match.group(1))
                        if 1 <= value <= MAX_PLAUSIBLE_LATENCY_MS:
                            return Perception(texts=(text,), latency_ms=value, ocr_confidence=confidence)
                if self.tracked_only:
                    texts = tuple(
                        str(item[0]).strip()
                        if isinstance(item[0], str)
                        else str(item[1]).strip()
                        for item in (results or [])
                        if isinstance(item, (list, tuple)) and item
                        and (isinstance(item[0], str) or len(item) >= 2)
                    )
                    return Perception(texts=texts)

        results, _ = self._engine(image)
        perceived, latency_box = self._parse(results)
        if latency_box is not None:
            height, width = image.shape[:2]
            x1, y1, x2, y2 = latency_box
            pad_x = max(4.0, (x2 - x1) * 0.08)
            pad_y = max(3.0, (y2 - y1) * 0.12)
            self._latency_roi = (
                max(0.0, (x1 - pad_x) / width),
                max(0.0, (y1 - pad_y) / height),
                min(1.0, (x2 + pad_x) / width),
                min(1.0, (y2 + pad_y) / height),
            )
        return perceived

    @staticmethod
    def _parse(results) -> tuple[Perception, Optional[tuple[float, float, float, float]]]:
        texts = []
        latency_candidates: list[tuple[float, int]] = []
        resolution_candidates: list[tuple[float, int]] = []
        best_confidence = 0.0
        latency_box: Optional[tuple[float, float, float, float]] = None
        for item in results or []:
            text = str(item[1]).strip()
            confidence = float(item[2])
            texts.append(text)
            best_confidence = max(best_confidence, confidence)
            cleaned = text.replace("毫秒", "ms")
            for match in LATENCY_RE.finditer(cleaned):
                value = int(match.group(1))
                if 1 <= value <= MAX_PLAUSIBLE_LATENCY_MS:
                    latency_candidates.append((confidence, value))
                    points = item[0]
                    xs = [float(point[0]) for point in points]
                    ys = [float(point[1]) for point in points]
                    latency_box = (min(xs), min(ys), max(xs), max(ys))
            upper = cleaned.upper().replace(" ", "")
            if "4K" in upper:
                resolution_candidates.append((confidence, 2160))
            for match in RESOLUTION_RE.finditer(cleaned):
                resolution_candidates.append((confidence, int(match.group(1))))

        joined = " ".join(texts).lower()
        latency = max(latency_candidates, default=(0.0, None))[1]
        resolution = max(resolution_candidates, default=(0.0, None))[1]
        candidate_confidences = [x[0] for x in latency_candidates + resolution_candidates]
        confidence = max(candidate_confidences, default=best_confidence)
        return Perception(
            texts=tuple(texts),
            latency_ms=latency,
            resolution=resolution,
            loading=any(word.lower() in joined for word in LOADING_WORDS),
            ocr_confidence=confidence,
        ), latency_box


class VLMPerception:
    """Optional OpenAI-compatible semantic classifier.

    It produces semantic state only. Numeric QoE values always come from OCR,
    motion or packet evidence so the VLM cannot invent measurements.
    """

    STATES = {"PRE_BUSINESS", "LOADING", "GAMEPLAY", "PLAYING", "LIVE", "MEETING", "CALL", "RESULT", "UNKNOWN"}

    def __init__(self, provider: str, base_url: str, api_key: str, model: str, timeout_sec: int = 30):
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_sec = timeout_sec

    def _endpoint(self) -> str:
        if self._base_url:
            if self._base_url.endswith("/chat/completions"):
                return self._base_url
            return self._base_url + "/chat/completions"
        if self._provider == "qwen":
            return "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        return "https://api.openai.com/v1/chat/completions"

    @classmethod
    def from_environment(cls) -> Optional["VLMPerception"]:
        if os.getenv("QOE_USE_VLM", "").lower() not in {"1", "true", "yes"}:
            return None
        provider = os.getenv("QOE_VLM_PROVIDER", "qwen").lower()
        base_url = os.getenv("QOE_VLM_BASE_URL", "")
        api_key = os.getenv("QOE_VLM_API_KEY", "")
        if not api_key:
            api_key = os.getenv("DASHSCOPE_API_KEY", "") if provider == "qwen" else os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            return None
        model = os.getenv("QOE_VLM_MODEL", "qwen-vl-plus" if provider == "qwen" else "gpt-4.1-mini")
        timeout = int(os.getenv("QOE_VLM_TIMEOUT_SEC", "30"))
        return cls(provider, base_url, api_key, model, timeout_sec=timeout)

    def classify(self, image: np.ndarray, app_type: str, scene: str) -> str:
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return "UNKNOWN"
        image_url = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        prompt = (
            "Classify whether the requested user business has started from this mobile screenshot. "
            f"app_type={app_type}, expected_scene={scene}. "
            "Return JSON only: {\"state\": one of PRE_BUSINESS, LOADING, GAMEPLAY, PLAYING, LIVE, "
            "MEETING, CALL, RESULT, UNKNOWN}."
        )
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
        }
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_sec) as response:
            body = json.loads(response.read().decode("utf-8", errors="ignore"))
        text = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if not match:
            return "UNKNOWN"
        try:
            state = str(json.loads(match.group(0)).get("state", "UNKNOWN")).upper()
        except (ValueError, TypeError):
            return "UNKNOWN"
        return state if state in self.STATES else "UNKNOWN"


def is_business_evidence(app_type: str, perception: Perception) -> bool:
    app = (app_type or "").lower()
    if perception.vlm_state in {"GAMEPLAY", "PLAYING", "LIVE", "MEETING", "CALL"}:
        return True
    if app in {"game", "mobile_game", "手游", "移动游戏"}:
        return perception.latency_ms is not None and not perception.loading
    if app in {"cloud_game", "cloud_gaming", "云游戏"}:
        return perception.vlm_state == "GAMEPLAY"
    return perception.vlm_state in {"PLAYING", "LIVE", "MEETING", "CALL"}
