"""Local test client — exercise the OCR pipeline without a TC22.

    python scripts/test_client.py path/to/label.jpg
    python scripts/test_client.py path/to/label.jpg --url http://host:8000
    python scripts/test_client.py --make-sample

Equivalent curl:

    curl -H "X-API-Key: <key>" -F "image=@label.jpg" \
         http://localhost:8000/api/v1/scans
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

SAMPLE_PATH = Path("sample_data/good/synthetic_label.jpg")


def make_sample() -> Path:
    """Render a synthetic label so the pipeline can be smoke-tested.

    This is a wiring check, not test data. Real accuracy work needs real
    D-Mart labels shot on the TC22 (sample_data/README.md).
    """
    import cv2
    import numpy as np

    image = np.full((640, 900, 3), 255, dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    lines = [
        ("D-MART SAMPLE PRODUCT", 60, 1.0),
        ("BATCH NO   A23C91", 160, 1.1),
        ("MFG        07/26", 250, 1.1),
        ("EXP        06/28", 340, 1.1),
        ("LOT        K18P2", 430, 1.1),
        ("MRP  Rs. 245.00", 530, 1.1),
    ]
    for text, y, scale in lines:
        cv2.putText(image, text, (40, y), font, scale, (0, 0, 0), 3, cv2.LINE_AA)

    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(SAMPLE_PATH), image)
    return SAMPLE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="Path to a label image")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--make-sample", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    if args.make_sample:
        path = make_sample()
        print(f"Wrote {path}")
        if not args.image:
            return 0

    image_path = Path(args.image) if args.image else SAMPLE_PATH
    if not image_path.exists():
        print(f"Not found: {image_path}", file=sys.stderr)
        print("Run with --make-sample to generate one.", file=sys.stderr)
        return 1

    settings = get_settings()
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}

    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{args.url}/api/v1/health")
        print(f"health {health.status_code}: {health.text}")

        with image_path.open("rb") as handle:
            response = client.post(
                f"{args.url}/api/v1/scans",
                files={"image": (image_path.name, handle, "image/jpeg")},
                data={"deviceId": "test-client", "deviceModel": "local"},
                headers=headers,
            )

    print(f"\nscans {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print(response.text)
        return 1

    print(json.dumps(body, indent=2)[:4000])

    if response.status_code == 200:
        print(f"\nstatus:  {body.get('status')}")
        print(f"variant: {body.get('variantUsed')}")
        print(f"timings: {body.get('timings')}")
        print(f"tokens:  {len(body.get('tokens', []))}")
        for token in body.get("tokens", []):
            print(f"  {token['confidence']:.3f}  {token['text']}")

    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
