import argparse
import json
from pathlib import Path

import cv2


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def parse_args():
    parser = argparse.ArgumentParser(description="Click the green self-circle center on enlarged minimap crops.")
    parser.add_argument("--image_dir", required=True, help="Folder containing screenshots to label")
    parser.add_argument("--out", default="annotations/minimap_self_centers.jsonl", help="JSONL output path")
    parser.add_argument("--roi_left", type=float, default=0.045)
    parser.add_argument("--roi_top", type=float, default=0.0)
    parser.add_argument("--roi_right", type=float, default=0.205)
    parser.add_argument("--roi_bottom", type=float, default=0.31)
    parser.add_argument("--scale", type=float, default=4.0, help="Display scale for the minimap crop")
    return parser.parse_args()


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def roi_bounds(image, args):
    height, width = image.shape[:2]
    left = int(width * clamp(args.roi_left, 0.0, 1.0))
    right = int(width * clamp(args.roi_right, 0.0, 1.0))
    top = int(height * clamp(args.roi_top, 0.0, 1.0))
    bottom = int(height * clamp(args.roi_bottom, 0.0, 1.0))
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height, bottom))
    return left, top, right, bottom


def list_images(image_dir):
    root = Path(image_dir)
    return sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def load_existing(out_path):
    labeled = set()
    if not out_path.exists():
        return labeled
    with out_path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            image_path = record.get("image_path")
            if image_path:
                labeled.add(str(Path(image_path).resolve()).lower())
    return labeled


def draw_overlay(display, click_point, text):
    view = display.copy()
    if click_point is not None:
        x, y = click_point
        cv2.drawMarker(view, (int(x), int(y)), (0, 0, 255), cv2.MARKER_CROSS, 22, 2)
        cv2.circle(view, (int(x), int(y)), 10, (0, 0, 255), 2)
    cv2.putText(view, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(view, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 1, cv2.LINE_AA)
    return view


def main():
    args = parse_args()
    image_paths = list_images(args.image_dir)
    if not image_paths:
        raise SystemExit(f"No images found in {args.image_dir}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labeled = load_existing(out_path)

    window_name = "Click self center on minimap ROI"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    records = []
    with out_path.open("a", encoding="utf-8") as out_file:
        for index, image_path in enumerate(image_paths, start=1):
            resolved_key = str(image_path.resolve()).lower()
            if resolved_key in labeled:
                continue

            image = cv2.imread(str(image_path))
            if image is None:
                print(f"Skip unreadable image: {image_path}")
                continue

            left, top, right, bottom = roi_bounds(image, args)
            roi = image[top:bottom, left:right]
            roi_height, roi_width = roi.shape[:2]
            display = cv2.resize(
                roi,
                (int(roi_width * args.scale), int(roi_height * args.scale)),
                interpolation=cv2.INTER_NEAREST,
            )

            click_point = None

            def on_mouse(event, x, y, flags, param):
                nonlocal click_point
                if event == cv2.EVENT_LBUTTONDOWN:
                    click_point = (x, y)

            cv2.setMouseCallback(window_name, on_mouse)
            prompt = f"{index}/{len(image_paths)} click center | Enter save | s skip | u undo | q quit"

            while True:
                cv2.imshow(window_name, draw_overlay(display, click_point, prompt))
                key = cv2.waitKey(40) & 0xFF
                if key in (13, 10) and click_point is not None:
                    display_x, display_y = click_point
                    roi_x = float(display_x) / float(args.scale)
                    roi_y = float(display_y) / float(args.scale)
                    abs_x = left + roi_x
                    abs_y = top + roi_y
                    record = {
                        "image_path": str(image_path.resolve()),
                        "image_width": int(image.shape[1]),
                        "image_height": int(image.shape[0]),
                        "minimap_roi": [int(left), int(top), int(right), int(bottom)],
                        "self_center_abs": [round(abs_x, 2), round(abs_y, 2)],
                        "self_center_roi_px": [round(roi_x, 2), round(roi_y, 2)],
                        "self_center_roi_ratio": [
                            round(roi_x / max(1.0, float(roi_width)), 4),
                            round(roi_y / max(1.0, float(roi_height)), 4),
                        ],
                    }
                    out_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out_file.flush()
                    records.append(record)
                    print(f"saved {image_path.name}: {record['self_center_roi_ratio']}")
                    break
                if key == ord("s"):
                    print(f"skipped {image_path.name}")
                    break
                if key == ord("u"):
                    click_point = None
                if key == ord("q"):
                    cv2.destroyAllWindows()
                    print(f"wrote {len(records)} new labels to {out_path}")
                    return

    cv2.destroyAllWindows()
    print(f"wrote {len(records)} new labels to {out_path}")


if __name__ == "__main__":
    main()
