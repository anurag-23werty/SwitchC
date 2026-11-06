"""Simple, distinct PNG avatars for Khata's agents: a coloured disc and a glyph."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

AGENT_LOOK = {
    "khata-dispatcher": ("#475569", "D"),
    "khata-intake": ("#0284c7", "I"),
    "khata-verifier": ("#7c3aed", "V"),
    "khata-analyst": ("#d97706", "A"),
    "khata-auditor": ("#be123c", "Au"),
    "khata-router": ("#0f766e", "R"),
    "khata-payer": ("#15803d", "P"),
}


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("Arial Bold.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def avatar_png(agent: str, size: int = 256) -> bytes:
    colour, glyph = AGENT_LOOK.get(agent, ("#334155", agent[:1].upper()))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size - 1, size - 1), fill=colour)
    font = _font(int(size * (0.42 if len(glyph) > 1 else 0.52)))
    box = d.textbbox((0, 0), glyph, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(
        ((size - w) / 2 - box[0], (size - h) / 2 - box[1]),
        glyph,
        font=font,
        fill="white",
    )
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
