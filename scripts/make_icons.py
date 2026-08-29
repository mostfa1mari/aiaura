"""Generate PWA raster icons from the AI AURA mark (dark tile + aura triangle).

    .venv/Scripts/python scripts/make_icons.py

Writes apple-touch-icon.png (180), icon-192.png, icon-512.png,
icon-192-maskable.png, icon-512-maskable.png into apps/web/.
"""

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
BG = (10, 11, 15, 255)
STOPS = [(0.0, (124, 92, 255)), (0.5, (34, 211, 238)), (1.0, (22, 199, 132))]


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def grad_color(t):
    for i in range(len(STOPS) - 1):
        t0, c0 = STOPS[i]
        t1, c1 = STOPS[i + 1]
        if t0 <= t <= t1:
            return lerp(c0, c1, (t - t0) / (t1 - t0))
    return STOPS[-1][1]


def render(size: int, maskable: bool = False) -> Image.Image:
    S = size * 4  # supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = 0 if maskable else int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=BG)

    cx = S / 2
    inset = 0.30 if maskable else 0.24   # keep art inside maskable safe zone
    top = S * inset
    bot = S * (1 - inset)
    half = (bot - top) * 0.30
    # aura diamond, filled with a vertical gradient
    for y in range(int(top), int(bot)):
        t = (y - top) / (bot - top)
        w = half * (1 - abs(2 * t - 1))  # 0 at tips, max at middle
        if w <= 0:
            continue
        d.line([(cx - w, y), (cx + w, y)], fill=grad_color(t) + (255,), width=1)

    if not maskable:
        r = S * 0.30
        d.ellipse([cx - r, cx - r, cx + r, cx + r], outline=grad_color(0.5) + (90,),
                  width=int(S * 0.012))
    return img.resize((size, size), Image.LANCZOS)


def main():
    render(180).save(WEB / "apple-touch-icon.png")
    render(192).save(WEB / "icon-192.png")
    render(512).save(WEB / "icon-512.png")
    render(192, maskable=True).save(WEB / "icon-192-maskable.png")
    render(512, maskable=True).save(WEB / "icon-512-maskable.png")
    print("icons written to", WEB)


if __name__ == "__main__":
    main()
