"""Screenshot helpers for MiniPilot.

The screenshot layer captures the current phone screen and prepares it for the
model layer. It returns both image metadata and base64 data.
"""

from __future__ import annotations

import base64
from io import BytesIO
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from mini_pilot.device import DeviceError, run_adb_bytes
from mini_pilot.timing import TIMING_CONFIG


@dataclass(frozen=True)
class Screenshot:
    """A captured phone screenshot."""

    path: Path
    width: int
    height: int
    base64_data: str
    mime_type: str = "image/png"
    is_mostly_black: bool = False


@dataclass(frozen=True)
class ScreenshotChange:
    """Simple visual difference metrics between two screenshots."""

    mean_difference: float
    changed_ratio: float


class ScreenshotError(RuntimeError):
    """Raised when screenshot capture or processing fails."""


def capture_screenshot(
    device_id: str | None = None,
    output_dir: str | Path | None = None,
    filename: str | None = None,
    timeout: int = 15,
    max_model_side: int = 2048,
    black_screen_retries: int = 2,
    black_screen_retry_delay: float | None = None,
) -> Screenshot:
    """Capture a screenshot from the connected Android device.

    Args:
        device_id: Optional ADB device id.
        output_dir: Directory where the screenshot should be saved. If omitted,
            a MiniPilot temp directory is used.
        filename: Optional file name. Defaults to a timestamped PNG name.
        timeout: ADB command timeout in seconds.
        max_model_side: Maximum image side length sent to the model. The
            returned width and height remain the real phone screen size so
            relative model coordinates still map to the device correctly.

    Returns:
        Screenshot metadata and base64 image data.
    """
    if black_screen_retry_delay is None:
        black_screen_retry_delay = TIMING_CONFIG.screenshot.black_screen_retry_delay

    target_path = _build_output_path(output_dir, filename)

    last_screenshot: Screenshot | None = None
    attempts = max(0, black_screen_retries) + 1

    for attempt in range(1, attempts + 1):
        screenshot = _capture_once(
            device_id=device_id,
            target_path=target_path,
            timeout=timeout,
            max_model_side=max_model_side,
        )
        last_screenshot = screenshot

        if not screenshot.is_mostly_black or attempt >= attempts:
            return screenshot

        time.sleep(max(0.0, black_screen_retry_delay))

    if last_screenshot is None:
        raise ScreenshotError("Failed to capture screenshot.")
    return last_screenshot


def load_screenshot(path: str | Path) -> Screenshot:
    """Load a screenshot file and return metadata plus base64 data."""
    image_path = Path(path)
    if not image_path.exists():
        raise ScreenshotError(f"Screenshot file does not exist: {image_path}")

    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except Exception as exc:
        raise ScreenshotError(f"Could not read screenshot image: {image_path}") from exc

    image_bytes = image_path.read_bytes()
    base64_data = base64.b64encode(image_bytes).decode("ascii")

    return Screenshot(
        path=image_path,
        width=width,
        height=height,
        base64_data=base64_data,
        mime_type=_guess_mime_type(image_path),
        is_mostly_black=_is_mostly_black_image(image_bytes),
    )


def _capture_once(
    device_id: str | None,
    target_path: Path,
    timeout: int,
    max_model_side: int,
) -> Screenshot:
    """Capture and process one screenshot attempt."""
    try:
        result = run_adb_bytes(
            ["exec-out", "screencap", "-p"],
            device_id=device_id,
            timeout=timeout,
            check=True,
        )
    except DeviceError as exc:
        raise ScreenshotError(f"Failed to capture screenshot: {exc}") from exc

    image_bytes = result.stdout
    if not image_bytes:
        raise ScreenshotError("ADB returned an empty screenshot.")

    try:
        image_bytes, original_width, original_height = _prepare_model_image(
            image_bytes=image_bytes,
            max_model_side=max_model_side,
        )
    except Exception as exc:
        raise ScreenshotError("Could not process screenshot image.") from exc

    target_path.write_bytes(image_bytes)
    base64_data = base64.b64encode(image_bytes).decode("ascii")
    is_mostly_black = _is_mostly_black_image(image_bytes)

    return Screenshot(
        path=target_path,
        width=original_width,
        height=original_height,
        base64_data=base64_data,
        mime_type="image/png",
        is_mostly_black=is_mostly_black,
    )


def _build_output_path(
    output_dir: str | Path | None = None, filename: str | None = None
) -> Path:
    """Build and create the local screenshot output path."""
    if output_dir is None:
        output_root = Path(tempfile.gettempdir()) / "mini_pilot"
    else:
        output_root = Path(output_dir)

    output_root.mkdir(parents=True, exist_ok=True)

    if filename is None:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"screenshot-{timestamp}.png"

    return output_root / filename


def _guess_mime_type(path: Path) -> str:
    """Return a simple MIME type based on file suffix."""
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "image/png"


def _prepare_model_image(
    image_bytes: bytes,
    max_model_side: int,
) -> tuple[bytes, int, int]:
    """Resize a screenshot for model limits while preserving screen metadata."""
    with Image.open(BytesIO(image_bytes)) as image:
        original_width, original_height = image.size
        longest_side = max(original_width, original_height)

        if max_model_side <= 0 or longest_side <= max_model_side:
            return image_bytes, original_width, original_height

        scale = max_model_side / longest_side
        resized_size = (
            max(1, int(original_width * scale)),
            max(1, int(original_height * scale)),
        )
        resized = image.resize(resized_size, Image.Resampling.LANCZOS)

        output = BytesIO()
        resized.save(output, format="PNG")
        return output.getvalue(), original_width, original_height


def _is_mostly_black_image(
    image_bytes: bytes,
    darkness_threshold: int = 16,
    black_ratio_threshold: float = 0.97,
    visible_ui_threshold: float = 0.01,
) -> bool:
    """Return whether a screenshot is unusably black.

    Dark apps such as short-video feeds can have a black video canvas while
    still showing usable navigation and loading UI. Treat those as valid
    screenshots so the agent can wait or recover instead of aborting.
    """
    with Image.open(BytesIO(image_bytes)) as image:
        grayscale = image.convert("L")
        width, height = grayscale.size

        full_sampled = grayscale.resize((128, 256), Image.Resampling.BILINEAR)
        full_pixels = list(full_sampled.getdata())
        if full_pixels:
            visible_pixels = sum(
                1 for pixel in full_pixels if pixel > darkness_threshold * 4
            )
            if visible_pixels / len(full_pixels) >= visible_ui_threshold:
                return False

        left = max(0, int(width * 0.08))
        top = max(0, int(height * 0.08))
        right = max(left + 1, int(width * 0.92))
        bottom = max(top + 1, int(height * 0.92))
        cropped = grayscale.crop((left, top, right, bottom))

        sampled = cropped.resize((64, 64), Image.Resampling.BILINEAR)
        pixels = list(sampled.getdata())
        if not pixels:
            return False

        dark_pixels = sum(1 for pixel in pixels if pixel <= darkness_threshold)
        return dark_pixels / len(pixels) >= black_ratio_threshold


def compare_screenshots(
    first: Screenshot,
    second: Screenshot,
    pixel_threshold: int = 24,
) -> ScreenshotChange:
    """Compare two screenshots and return simple visual change metrics."""
    with Image.open(first.path) as image_a, Image.open(second.path) as image_b:
        grayscale_a = image_a.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
        grayscale_b = image_b.convert("L").resize((64, 64), Image.Resampling.BILINEAR)

        pixels_a = list(grayscale_a.getdata())
        pixels_b = list(grayscale_b.getdata())
        if not pixels_a or not pixels_b or len(pixels_a) != len(pixels_b):
            return ScreenshotChange(mean_difference=0.0, changed_ratio=0.0)

        differences = [abs(a - b) for a, b in zip(pixels_a, pixels_b)]
        mean_difference = sum(differences) / len(differences) / 255.0
        changed_ratio = (
            sum(1 for item in differences if item >= pixel_threshold) / len(differences)
        )
        return ScreenshotChange(
            mean_difference=mean_difference,
            changed_ratio=changed_ratio,
        )
