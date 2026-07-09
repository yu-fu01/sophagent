#!/usr/bin/env python3
"""
Marketing poster/image generation via SophNet Gemini API.

Generates marketing posters and promotional images for various platforms
(WeChat Moments, Xiaohongshu, WeChat Official Account, etc.), uploads to OSS,
and returns a public URL. Supports style presets, fine-grained style dimensions,
text overlay, and reference images (product photos, logos, etc.).

Usage:
    python generate_poster.py --type moments --prompt "..." [--style-preset promo]
    python generate_poster.py --type xiaohongshu --style-preset kawaii --prompt "..."
    python generate_poster.py --type moments --prompt "..." --text-overlay '{"title":"母亲节特惠"}'
    python generate_poster.py --type moments --prompt "..." --reference-image product.jpg --reference-image logo.png
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import tempfile
from typing import Any

import requests

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

API_URL = (
    "https://www.sophnet.com/api/open-apis/projects/easyllms/imagegenerator/"
    "google/models/gemini-3.1-flash-image-preview:generateContent"
)

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

SAFETY_NEGATIVE_PROMPT = (
    "nsfw, nudity, nude, naked, sexual, erotic, pornographic, gore, blood, violence, "
    "bloody, corpse, dead body, weapon, gun, knife, drugs, smoking, alcohol, gambling, "
    "politically sensitive, national flag, national emblem, political leader, "
    "religious symbol, hate symbol, discrimination, racist, offensive, disturbing, "
    "child exploitation, terrorism, self-harm"
)

# ---------------------------------------------------------------------------
# Poster types
# ---------------------------------------------------------------------------

POSTER_TYPES: dict[str, dict[str, str]] = {
    "moments": {
        "size": "1080*1080",
        "aspect_ratio": "1:1",
        "label": "朋友圈海报 (1080×1080)",
    },
    "xiaohongshu": {
        "size": "1080*1440",
        "aspect_ratio": "3:4",
        "label": "小红书封面 (1080×1440)",
    },
    "wechat-header": {
        "size": "900*383",
        "aspect_ratio": "16:9",
        "label": "公众号头图 (900×383)",
    },
    "wechat-square": {
        "size": "200*200",
        "aspect_ratio": "1:1",
        "label": "公众号方形预览 (200×200)",
    },
    "share-card": {
        "size": "500*400",
        "aspect_ratio": "4:3",
        "label": "微信群分享图 (500×400)",
    },
    "product": {
        "size": "1024*1024",
        "aspect_ratio": "1:1",
        "label": "产品展示图 (1024×1024)",
    },
    "guide": {
        "size": "1080*1440",
        "aspect_ratio": "3:4",
        "label": "攻略图/信息图 (1080×1440)",
    },
}

# ---------------------------------------------------------------------------
# Style dimensions
# ---------------------------------------------------------------------------

PALETTES: dict[str, str] = {
    "warm": "Warm color palette: golden yellows, amber oranges, terracotta reds, honey tones. Evokes comfort and warmth.",
    "elegant": "Elegant palette: champagne gold, ivory, dusty rose, soft grey. Refined and sophisticated feel.",
    "cool": "Cool palette: ocean blues, mint greens, silver greys, icy whites. Clean and calming atmosphere.",
    "dark": "Dark palette: deep navy, charcoal, dark teal, muted burgundy. High contrast, dramatic mood.",
    "earth": "Earth palette: olive green, clay brown, sandstone, forest tones. Natural and organic feeling.",
    "vivid": "Vivid palette: saturated primary colors, bold contrasts, bright accents. High energy and eye-catching.",
    "pastel": "Pastel palette: soft pink, baby blue, lavender, mint. Gentle, dreamy, and delicate.",
    "mono": "Monochrome palette: shades of a single hue with tonal variation. Unified and striking.",
    "retro": "Retro palette: mustard yellow, burnt orange, avocado green, faded teal. 1970s nostalgic warmth.",
}

RENDERINGS: dict[str, str] = {
    "flat-vector": "Flat vector illustration style: clean geometric shapes, solid color fills, no gradients or textures, minimal line work.",
    "hand-drawn": "Hand-drawn illustration style: visible sketch lines, organic imperfections, ink-and-paper feel, slightly uneven edges.",
    "painterly": "Painterly style: visible brush strokes, rich textures, soft blended edges, oil or watercolor painting feel.",
    "digital": "Polished digital art style: smooth gradients, clean rendering, precise details, modern and refined.",
    "pixel": "Pixel art style: retro 8-bit aesthetic, blocky shapes, limited color palette, nostalgic gaming feel.",
    "chalk": "Chalk/chalkboard style: white and colored chalk on dark background, hand-lettered feel, educational aesthetic.",
}

MOODS: dict[str, str] = {
    "subtle": "Subtle mood: low contrast, muted tones, generous whitespace, understated and calm composition.",
    "balanced": "Balanced mood: moderate contrast, harmonious composition, professional and approachable.",
    "bold": "Bold mood: high contrast, saturated colors, dynamic composition, strong visual impact.",
}

LAYOUTS: dict[str, str] = {
    "bento-grid": "Bento grid layout: modular grid of varied-size cards, each containing a distinct piece of information.",
    "list": "List layout: enumerated items in a vertical sequence with icons or numbers.",
    "comparison": "Comparison layout: side-by-side or split-screen contrasting two options or concepts.",
    "flow": "Flow layout: connected steps or stages showing a process or timeline with directional arrows.",
    "mindmap": "Mind map layout: central concept radiating outward to connected branches and sub-topics.",
    "hub-spoke": "Hub-spoke layout: central element surrounded by related items in a radial arrangement.",
    "funnel": "Funnel layout: wide-to-narrow stages showing progressive filtering or conversion.",
    "dense-modules": "Dense modules layout: tightly packed information blocks with high data density, guide-style.",
}

STYLE_PRESETS: dict[str, dict[str, str]] = {
    "promo": {"palette": "vivid", "rendering": "flat-vector", "mood": "bold"},
    "elegant": {"palette": "earth", "rendering": "hand-drawn", "mood": "subtle"},
    "minimal": {"palette": "mono", "rendering": "flat-vector", "mood": "subtle"},
    "festive": {"palette": "warm", "rendering": "painterly", "mood": "bold"},
    "cozy": {"palette": "warm", "rendering": "painterly", "mood": "subtle"},
    "kawaii": {"palette": "pastel", "rendering": "flat-vector", "mood": "balanced"},
    "morandi": {"palette": "earth", "rendering": "hand-drawn", "mood": "subtle"},
    "pop-art": {"palette": "vivid", "rendering": "flat-vector", "mood": "bold"},
    "vintage": {"palette": "retro", "rendering": "hand-drawn", "mood": "balanced"},
    "blueprint": {"palette": "dark", "rendering": "chalk", "mood": "bold"},
    "notion": {"palette": "mono", "rendering": "hand-drawn", "mood": "subtle"},
    "watercolor": {"palette": "pastel", "rendering": "painterly", "mood": "subtle"},
    "corporate": {"palette": "cool", "rendering": "flat-vector", "mood": "balanced"},
}

STANDARD_RATIOS = ["1:1", "3:4", "4:3", "9:16", "16:9"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_size(size_str: str) -> tuple[int, int]:
    """Parse '1080x1440', '1080*1440', or '1080×1440' into (width, height)."""
    normalized = size_str.replace("x", "*").replace("×", "*")
    parts = normalized.split("*")
    if len(parts) != 2:
        raise ValueError(f"Invalid size format: {size_str!r}. Use WxH (e.g. 1080x1440).")
    return int(parts[0].strip()), int(parts[1].strip())


def size_to_aspect_ratio(w: int, h: int) -> str:
    target = w / h
    best = "1:1"
    best_diff = float("inf")
    for ratio_str in STANDARD_RATIOS:
        rw, rh = map(int, ratio_str.split(":"))
        diff = abs(math.log(target) - math.log(rw / rh))
        if diff < best_diff:
            best_diff = diff
            best = ratio_str
    return best


def size_to_image_size(w: int, h: int) -> str:
    return "512" if max(w, h) <= 512 else "1K"


def resolve_style(
    preset: str | None,
    palette: str | None,
    rendering: str | None,
    mood: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Apply preset defaults, then override with explicit values."""
    p_pal, p_ren, p_mood = None, None, None
    if preset and preset in STYLE_PRESETS:
        cfg = STYLE_PRESETS[preset]
        p_pal, p_ren, p_mood = cfg.get("palette"), cfg.get("rendering"), cfg.get("mood")
    return (palette or p_pal, rendering or p_ren, mood or p_mood)


ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_REFERENCE_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB


def load_reference_image(path: str) -> dict[str, str]:
    """Read a local image file and return an inline_data dict for the API."""
    from pathlib import Path

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Reference image not found: {path}")

    ext = p.suffix.lower()
    mime = ALLOWED_IMAGE_EXTENSIONS.get(ext)
    if not mime:
        raise ValueError(
            f"Unsupported image format '{ext}' for {path}. "
            f"Supported: {', '.join(ALLOWED_IMAGE_EXTENSIONS)}"
        )

    size = p.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image too large ({size / 1024 / 1024:.1f} MB): {path}. "
            f"Max {MAX_IMAGE_BYTES / 1024 / 1024:.0f} MB."
        )

    raw = p.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return {"mime_type": mime, "data": b64}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_prompt(
    user_prompt: str,
    width: int,
    height: int,
    palette: str | None = None,
    rendering: str | None = None,
    mood: str | None = None,
    layout: str | None = None,
    text_overlay: dict[str, str] | None = None,
    negative_prompt: str | None = None,
    has_reference_images: bool = False,
) -> str:
    merged_negative = SAFETY_NEGATIVE_PROMPT
    if negative_prompt:
        merged_negative = f"{SAFETY_NEGATIVE_PROMPT}, {negative_prompt}"

    parts = [
        "Generate a marketing poster image with these specifications:",
        f"- Desired dimensions: {width}x{height} pixels",
    ]

    if has_reference_images:
        parts.append(
            "- Reference images are provided. Incorporate the products, logos, "
            "or elements shown in the reference images into the poster with "
            "high fidelity. Preserve key visual details (colors, shapes, text "
            "on products) from the reference images."
        )

    if palette and palette in PALETTES:
        parts.append(f"- Color direction: {PALETTES[palette]}")
    if rendering and rendering in RENDERINGS:
        parts.append(f"- Rendering style: {RENDERINGS[rendering]}")
    if mood and mood in MOODS:
        parts.append(f"- Mood: {MOODS[mood]}")
    if layout and layout in LAYOUTS:
        parts.append(f"- Information layout: {LAYOUTS[layout]}")

    if text_overlay:
        text_parts = []
        if text_overlay.get("title"):
            text_parts.append(f'main title text "{text_overlay["title"]}"')
        if text_overlay.get("subtitle"):
            text_parts.append(f'subtitle text "{text_overlay["subtitle"]}"')
        if text_overlay.get("price"):
            text_parts.append(f'prominent price tag "{text_overlay["price"]}"')
        if text_overlay.get("footer"):
            text_parts.append(f'footer text "{text_overlay["footer"]}"')
        if text_parts:
            parts.append(f"- Text to render on the image: {', '.join(text_parts)}. "
                         "Text must be clearly legible, well-positioned, and not overlap key visual elements.")

    parts.extend([
        f"- {user_prompt}",
        f"- Content safety: The image must NOT contain any of the following: {merged_negative}.",
        "Generate only the image, no extra commentary.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------


def call_gemini(
    api_key: str,
    prompt: str,
    aspect_ratio: str,
    image_size: str,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    parts: list[dict[str, Any]] = [{"text": prompt}]
    if reference_images:
        for img in reference_images:
            parts.append({"inline_data": img})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": image_size,
            },
        },
    }
    resp = requests.post(API_URL, json=payload, headers=headers, timeout=300)
    resp.raise_for_status()
    return resp.json()


def extract_image_b64(data: dict[str, Any]) -> tuple[str | None, str | None]:
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and "data" in inline:
                mime = inline.get("mimeType", inline.get("mime_type", "image/png"))
                ext = mime.split("/")[-1].split(";")[0]
                return inline["data"], ext
    return None, None


def upload_b64_image(b64_data: str, ext: str = "png") -> str | None:
    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:
        print(f"Warning: base64 decode failed: {e}", file=sys.stderr)
        return None

    fd, tmp_path = tempfile.mkstemp(suffix=f".{ext}", prefix="poster_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        import sophnet_tools
        signed_url = sophnet_tools.upload_oss(tmp_path)
        if not signed_url:
            print("Warning: upload_oss returned no signed URL", file=sys.stderr)
            return None
        return signed_url
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_output(
    poster_type: str,
    w: int,
    h: int,
    palette: str | None,
    rendering: str | None,
    mood: str | None,
    layout: str | None,
    style_preset: str | None,
    status: str,
    image_url: str | None,
    reference_image_count: int = 0,
    fmt: str = "text",
) -> str:
    data = {
        "POSTER_TYPE": poster_type,
        "POSTER_SIZE": f"{w}*{h}",
    }
    if style_preset:
        data["STYLE_PRESET"] = style_preset
    if palette:
        data["PALETTE"] = palette
    if rendering:
        data["RENDERING"] = rendering
    if mood:
        data["MOOD"] = mood
    if layout:
        data["LAYOUT"] = layout
    if reference_image_count:
        data["REFERENCE_IMAGES"] = str(reference_image_count)
    data["STATUS"] = status
    if image_url:
        data["IMAGE_URL"] = image_url

    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    return "\n".join(f"{k}={v}" for k, v in data.items())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    type_names = ", ".join(f"{k} ({v['label']})" for k, v in POSTER_TYPES.items())
    preset_names = ", ".join(STYLE_PRESETS.keys())

    parser = argparse.ArgumentParser(
        description="Generate marketing posters via SophNet Gemini API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Poster types:\n  {type_names}\n\n"
            f"Style presets (shorthand for palette+rendering+mood):\n  {preset_names}\n\n"
            f"Palettes:\n  {', '.join(PALETTES.keys())}\n\n"
            f"Renderings:\n  {', '.join(RENDERINGS.keys())}\n\n"
            f"Moods:\n  {', '.join(MOODS.keys())}\n\n"
            f"Layouts (guide type only):\n  {', '.join(LAYOUTS.keys())}"
        ),
    )
    parser.add_argument("--type", required=True, choices=POSTER_TYPES.keys(),
                        help="Poster type (determines default size and aspect ratio)")
    parser.add_argument("--prompt", required=True,
                        help="Image content description (scene, subject, atmosphere)")
    parser.add_argument("--style-preset", default=None, choices=list(STYLE_PRESETS.keys()),
                        help="Style shorthand that sets palette+rendering+mood together")
    parser.add_argument("--palette", default=None, choices=list(PALETTES.keys()),
                        help="Color palette direction (overrides preset)")
    parser.add_argument("--rendering", default=None, choices=list(RENDERINGS.keys()),
                        help="Visual rendering style (overrides preset)")
    parser.add_argument("--mood", default=None, choices=list(MOODS.keys()),
                        help="Overall mood intensity (overrides preset)")
    parser.add_argument("--layout", default=None, choices=list(LAYOUTS.keys()),
                        help="Information layout (primarily for guide type)")
    parser.add_argument("--text-overlay", default=None,
                        help='Text to render on poster, JSON format: \'{"title":"...", "price":"..."}\'')
    parser.add_argument("--size", default=None,
                        help="Override size as WxH or W*H (default: auto from --type)")
    parser.add_argument("--reference-image", action="append", default=None,
                        help="Path to a reference image (product photo, logo, etc.). "
                             "Can be specified multiple times, up to 10 images.")
    parser.add_argument("--negative-prompt", default=None,
                        help="Additional negative prompt terms")
    parser.add_argument("--format", default="text", choices=["text", "json"],
                        help="Output format (default: text)")
    args = parser.parse_args(argv)

    # Resolve style
    palette, rendering, mood = resolve_style(
        args.style_preset, args.palette, args.rendering, args.mood,
    )

    # Resolve size
    poster_cfg = POSTER_TYPES[args.type]
    try:
        w, h = parse_size(args.size) if args.size else parse_size(poster_cfg["size"])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    aspect_ratio = poster_cfg["aspect_ratio"] if not args.size else size_to_aspect_ratio(w, h)
    image_size = size_to_image_size(w, h)

    # Parse text overlay
    text_overlay = None
    if args.text_overlay:
        try:
            text_overlay = json.loads(args.text_overlay)
        except json.JSONDecodeError as e:
            print(f"Error: --text-overlay is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # Load reference images
    ref_images: list[dict[str, str]] = []
    if args.reference_image:
        if len(args.reference_image) > MAX_REFERENCE_IMAGES:
            print(
                f"Error: too many reference images ({len(args.reference_image)}). "
                f"Max {MAX_REFERENCE_IMAGES}.",
                file=sys.stderr,
            )
            sys.exit(1)
        for img_path in args.reference_image:
            try:
                ref_images.append(load_reference_image(img_path))
            except (FileNotFoundError, ValueError) as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

    # Get API key
    import sophnet_tools
    api_key = sophnet_tools.get_api_key()
    if not api_key:
        print("Error: No API key found. Set SOPH_API_KEY or configure via sophnet-key skill.",
              file=sys.stderr)
        sys.exit(1)

    # Build prompt and call API
    prompt = build_prompt(
        args.prompt, w, h,
        palette=palette, rendering=rendering, mood=mood,
        layout=args.layout, text_overlay=text_overlay,
        negative_prompt=args.negative_prompt,
        has_reference_images=bool(ref_images),
    )
    print("STATUS=generating", file=sys.stderr)

    try:
        result = call_gemini(
            api_key, prompt, aspect_ratio, image_size,
            reference_images=ref_images or None,
        )
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code in (401, 403):
            print("STATUS=permission_denied", file=sys.stderr)
            print(
                "Error: 当前账户没有所调用图片生成模型的使用权限。"
                "请联系 Sophclaw 平台客服开通权限后重试。",
                file=sys.stderr,
            )
        else:
            print(f"STATUS=api_error", file=sys.stderr)
            print(f"Error: SophNet Gemini API call failed (HTTP {status_code}): {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print("STATUS=api_error", file=sys.stderr)
        print(f"Error: SophNet Gemini API call failed: {e}", file=sys.stderr)
        sys.exit(1)

    b64_data, ext = extract_image_b64(result)
    if not b64_data:
        print("Error: no image data found in response.", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)

    image_url = upload_b64_image(b64_data, ext or "png")
    if not image_url:
        print("Error: failed to upload image to OSS.", file=sys.stderr)
        sys.exit(1)

    print(format_output(
        poster_type=args.type, w=w, h=h,
        palette=palette, rendering=rendering, mood=mood,
        layout=args.layout, style_preset=args.style_preset,
        status="succeeded", image_url=image_url,
        reference_image_count=len(ref_images),
        fmt=args.format,
    ))


if __name__ == "__main__":
    main()
