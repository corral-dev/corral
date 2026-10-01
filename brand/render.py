"""Derive app and public presentation assets from a single approved brand source."""

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def rounded(image, size):
    result = image.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", result.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), size * 0.22, fill=255)
    result.putalpha(mask)
    return result


def font(size):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    source = root / "brand/source.png"
    with Image.open(source) as image:
        if image.width != image.height:
            raise ValueError("The approved source must be square.")
        master = image.convert("RGB").resize((1024, 1024), Image.Resampling.LANCZOS)
    master_path = root / "brand/AppIcon-1024.png"
    master.save(master_path, optimize=True)
    display = rounded(master, 224)
    display_path = root / "docs/screenshots/corral-icon.png"
    display.save(display_path, optimize=True)
    # Preserve the existing sanitized product capture as evidence on the card.
    capture = root / "docs/screenshots/list.png"
    card = Image.new("RGB", (1280, 640), "#F6F8F5")
    badge = rounded(master, 224)
    card.paste(badge, (44, 122), badge)
    draw = ImageDraw.Draw(card)
    draw.text((45, 362), "Corral", font=font(50), fill="#256B4C")
    draw.multiline_text((47, 438), "All your coding agents.\nOne terminal.", font=font(20), fill="#3B4B40", spacing=8)
    with Image.open(capture) as screenshot:
        screenshot = screenshot.convert("RGB")
        screenshot.thumbnail((920, 568), Image.Resampling.LANCZOS)
        card.paste(screenshot, (332, (640 - screenshot.height) // 2))
    card_path = root / "docs/screenshots/social-preview.png"
    card.save(card_path, optimize=True)
    outputs = [master_path, display_path, card_path]
    manifest = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "background": "#FFF5D9",
        "optical_shift_px": [9, -6],
        "refined_hat_sha256": hashlib.sha256((root / "brand/refined-hat.png").read_bytes()).hexdigest(),
        "artwork": "Approved yellow-green cowboy hat; cream yellow #FFF5D9; optical placement",
        "generation": "Built-in imagegen refinement; approved deterministic background and optical placement",
        "outputs": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in outputs},
    }
    (root / "brand/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
