#!/usr/bin/env python3
"""Draw MqttChat's app icons.

Run from the project directory: `python3 make-icons.py`

The mark is a speech bubble with three dots, on the slate the page's title and buttons use.
`any` icons are a rounded square the platform may show as-is; the `maskable` one fills its
square edge to edge with the mark well inside the safe zone, because Android crops maskable
icons to whatever shape the launcher likes. Sizes follow what the platforms ask for: 192 and
512 for the manifest, 180 for iOS's home screen, 32 for a browser tab.

Pillow is the only dependency, and it is a build-time one: nothing here ships.
"""
from PIL import Image, ImageDraw

SLATE = (31, 59, 87, 255)      # the page's theme colour
PAPER = (253, 253, 253, 255)   # the answer panel's background
SIZE = 1024                     # everything is drawn at this size and scaled down


def rounded_square(size, radius, colour):
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=colour)
    return image


def bubble(image, left, top, right, bottom, tail_x, colour):
    """A speech bubble: a rounded rectangle with a triangular tail at the bottom left."""
    draw = ImageDraw.Draw(image)
    radius = (bottom - top) // 3
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=colour)
    draw.polygon([(tail_x, bottom - 6), (tail_x - 54, bottom + 150), (tail_x + 128, bottom - 6)], fill=colour)
    return image


def with_dots(image, colour, count=3):
    """Three dots inside the bubble: the mark that says 'a conversation'."""
    draw = ImageDraw.Draw(image)
    width, height = image.size
    radius = width // 34
    spacing = width // 7
    centre_x = width // 2
    centre_y = int(height * 0.44)
    first = centre_x - spacing * (count - 1) // 2
    for index in range(count):
        x = first + index * spacing
        draw.ellipse((x - radius, centre_y - radius, x + radius, centre_y + radius), fill=colour)
    return image


def mark(size, inset, rounded, background=SLATE, foreground=PAPER):
    """One finished icon: a background plus the mark, inset so masks cannot clip it."""
    image = rounded_square(size, size // 4 if rounded else 0, background) if rounded else \
        Image.new('RGBA', (size, size), background)
    left = int(size * inset)
    right = size - left
    top = int(size * (inset + 0.05))
    bottom = int(size * (1 - inset - 0.10))
    bubble(image, left, top, right, bottom, left + int(size * 0.10), foreground)
    return with_dots(image, background)


def main():
    # `any`: a rounded square, comfortable to look at in a launcher and in a task switcher.
    for size in (192, 512):
        mark(SIZE, 0.12, rounded=True).resize((size, size), Image.LANCZOS).save(f'icon-{size}.png')
    # `maskable`: full bleed, mark inside the middle 80% so any crop shape still shows it.
    mark(SIZE, 0.22, rounded=False).resize((512, 512), Image.LANCZOS).save('icon-maskable-512.png')
    # iOS home screen: it rounds the corners itself, so square corners are right here.
    mark(SIZE, 0.14, rounded=False).resize((180, 180), Image.LANCZOS).save('apple-touch-icon.png')
    # A browser tab.
    mark(SIZE, 0.10, rounded=True).resize((32, 32), Image.LANCZOS).save('favicon.png')
    print('wrote icon-192.png icon-512.png icon-maskable-512.png apple-touch-icon.png favicon.png')


if __name__ == '__main__':
    main()
