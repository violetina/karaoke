import math
import os
import random
from PIL import Image, ImageDraw

BAYER4 = [
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5]
]

def dither_val(v, x, y):
    threshold = (BAYER4[y % 4][x % 4] + 0.5) / 16.0
    return v + (threshold - 0.5) * 0.25

def clamp(v, lo=0, hi=255):
    return max(lo, min(hi, int(v)))

def lerp_rgb(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t)
    )

def make_gradient(c_top, c_bot, curve=1.0):
    im = Image.new("RGB", (64, 64))
    pixels = im.load()
    for y in range(64):
        t = (y / 63.0) ** curve
        for x in range(64):
            dt = dither_val(t, x, y)
            pixels[x, y] = lerp_rgb(c_top, c_bot, dt)
    return im

def draw_stars(im, count=35, colors=[(255, 255, 255), (200, 220, 255), (255, 240, 200)], rng=None):
    r = rng or random.Random(42)
    pixels = im.load()
    for _ in range(count):
        x = r.randint(0, 63)
        y = r.randint(0, 40)
        c = r.choice(colors)
        pixels[x, y] = c
        if r.random() < 0.2 and 0 < x < 63 and 0 < y < 63:
            # cross twinkle
            dim = lerp_rgb(c, (0, 0, 0), 0.5)
            pixels[x+1, y] = dim
            pixels[x-1, y] = dim
            pixels[x, y+1] = dim
            pixels[x, y-1] = dim

def draw_moon(im, cx, cy, rad, color=(255, 245, 200), crescent=True, bg_color=None):
    draw = ImageDraw.Draw(im)
    draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=color)
    if crescent:
        bg = bg_color or (30, 20, 50)
        draw.ellipse([cx - rad + int(rad*0.5), cy - rad - int(rad*0.2), cx + rad + int(rad*0.4), cy + rad], fill=bg)

# --- 1. UPLIFTING 1 & 2 ---
def make_uplifting_1():
    # Soaring golden sunbeams rising over mountain peaks
    im = make_gradient((30, 70, 160), (255, 210, 120), curve=1.2)
    pixels = im.load()
    # Sunbeams
    for x in range(64):
        for y in range(48):
            angle = math.atan2(y - 48, x - 32)
            beam = (math.sin(angle * 8) + 1) * 0.5
            if beam > 0.6:
                t = (beam - 0.6) * 0.4
                pixels[x, y] = lerp_rgb(pixels[x, y], (255, 250, 210), t)
    draw = ImageDraw.Draw(im)
    # Distant mountain
    draw.polygon([(0, 63), (18, 38), (35, 48), (52, 34), (63, 44), (63, 63)], fill=(45, 30, 80))
    # Fore mountain
    draw.polygon([(0, 63), (28, 42), (48, 63)], fill=(25, 18, 55))
    draw.polygon([(25, 63), (55, 45), (63, 52), (63, 63)], fill=(18, 12, 40))
    # Soaring birds
    for bx, by in [(24, 20), (32, 14), (38, 18), (45, 25)]:
        pixels[bx, by] = (255, 255, 230)
        pixels[bx-1, by-1] = (255, 240, 200)
        pixels[bx+1, by-1] = (255, 240, 200)
    return im

def make_uplifting_2():
    # Ascending particle light spiral towards sky
    im = make_gradient((15, 35, 90), (70, 170, 230))
    pixels = im.load()
    draw_stars(im, count=25, rng=random.Random(101))
    for i in range(120):
        t = i / 120.0
        angle = t * math.pi * 5.0
        rad = (1.0 - t) * 26.0
        x = clamp(32 + math.cos(angle) * rad)
        y = clamp(58 - t * 48)
        c = lerp_rgb((255, 200, 80), (255, 255, 255), t)
        pixels[x, y] = c
        if 0 < x < 63 and 0 < y < 63 and i % 3 == 0:
            pixels[x+1, y] = lerp_rgb(c, (0,0,0), 0.4)
            pixels[x, y+1] = lerp_rgb(c, (0,0,0), 0.4)
    return im

# --- 2. INTENSE 1 & 2 ---
def make_intense_1():
    # Crimson-black pulsing vortex with shockwaves
    im = Image.new("RGB", (64, 64))
    pixels = im.load()
    for y in range(64):
        for x in range(64):
            dx = (x - 32)
            dy = (y - 32)
            dist = math.sqrt(dx*dx + dy*dy)
            angle = math.atan2(dy, dx)
            spiral = (angle + dist * 0.3) % (math.pi / 2)
            val = math.sin(spiral * 4) * 0.5 + 0.5
            fade = max(0.0, 1.0 - dist / 34.0)
            c = lerp_rgb((15, 0, 5), (240, 20, 40), val * fade)
            if dist < 6:
                c = (255, 220, 220)
            pixels[x, y] = c
    # Ember sparks
    r = random.Random(666)
    for _ in range(40):
        x = r.randint(6, 57)
        y = r.randint(6, 57)
        pixels[x, y] = r.choice([(255, 120, 30), (255, 240, 180), (220, 30, 40)])
    return im

def make_intense_2():
    # High energy molten geometric fracture
    im = make_gradient((10, 0, 10), (80, 10, 20))
    pixels = im.load()
    draw = ImageDraw.Draw(im)
    # Sharp fracture lines
    coords = [(0, 32), (18, 25), (28, 38), (42, 18), (63, 28)]
    draw.line(coords, fill=(255, 240, 150), width=2)
    coords2 = [(28, 38), (35, 55), (55, 63)]
    draw.line(coords2, fill=(255, 120, 30), width=1)
    coords3 = [(18, 25), (22, 5), (10, 0)]
    draw.line(coords3, fill=(255, 80, 20), width=1)
    # Radiating glow around center fracture
    for _ in range(50):
        rx = random.randint(15, 48)
        ry = random.randint(15, 48)
        pixels[rx, ry] = (255, random.randint(180, 255), random.randint(50, 120))
    return im

# --- 3. DRAMATIC 1 & 2 ---
def make_dramatic_1():
    # Grand theatre spotlight on empty stage with deep violet curtains
    im = make_gradient((18, 8, 32), (40, 15, 65))
    pixels = im.load()
    # Spotlight cone
    for y in range(64):
        beam_w = 4 + (y / 63.0) * 28
        for x in range(64):
            if abs(x - 32) < beam_w:
                t = 1.0 - (abs(x - 32) / beam_w)
                dt = dither_val(t * 0.7, x, y)
                pixels[x, y] = lerp_rgb(pixels[x, y], (255, 240, 170), dt)
    draw = ImageDraw.Draw(im)
    # Curtains on sides
    draw.polygon([(0, 0), (14, 0), (10, 63), (0, 63)], fill=(80, 10, 40))
    draw.polygon([(63, 0), (49, 0), (53, 63), (63, 63)], fill=(80, 10, 40))
    # Stage floor
    draw.polygon([(0, 52), (63, 52), (63, 63), (0, 63)], fill=(25, 12, 20))
    # Spotlight oval on floor
    draw.ellipse([18, 54, 46, 62], fill=(255, 245, 190))
    return im

def make_dramatic_2():
    # Dual cross spotlights cutting through night sky with opera balcony silhouette
    im = make_gradient((8, 5, 20), (25, 18, 45))
    pixels = im.load()
    for y in range(55):
        # Beam 1
        b1_x = 10 + y * 0.8
        # Beam 2
        b2_x = 54 - y * 0.8
        for x in range(64):
            d1 = abs(x - b1_x)
            d2 = abs(x - b2_x)
            if d1 < 5:
                pixels[x, y] = lerp_rgb(pixels[x, y], (255, 230, 160), (1 - d1/5.0)*0.5)
            if d2 < 5:
                pixels[x, y] = lerp_rgb(pixels[x, y], (180, 220, 255), (1 - d2/5.0)*0.5)
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 52, 63, 63], fill=(15, 10, 25))
    # Balcony railing silhouette
    for rx in range(0, 64, 4):
        draw.rectangle([rx, 46, rx+1, 52], fill=(210, 180, 100))
    draw.line([(0, 46), (63, 46)], fill=(210, 180, 100), width=1)
    return im

# --- 4. WISTFUL 1 & 2 ---
def make_wistful_1():
    # Autumn leaves blowing past a cozy window at dusk
    im = make_gradient((45, 25, 60), (180, 110, 90))
    draw_moon(im, 48, 18, 7, color=(255, 235, 190), crescent=True, bg_color=(45, 25, 60))
    pixels = im.load()
    draw_stars(im, count=15, rng=random.Random(88))
    # Falling leaves in wind
    r = random.Random(77)
    leaf_colors = [(220, 120, 40), (200, 70, 30), (235, 170, 50), (160, 50, 40)]
    for _ in range(25):
        lx = r.randint(2, 61)
        ly = r.randint(10, 61)
        c = r.choice(leaf_colors)
        pixels[lx, ly] = c
        if r.random() < 0.5:
            pixels[lx+1, ly] = lerp_rgb(c, (0,0,0), 0.3)
    # Window frame silhouette
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, 63, 3], fill=(30, 20, 35))
    draw.rectangle([0, 0, 3, 63], fill=(30, 20, 35))
    draw.rectangle([60, 0, 63, 63], fill=(30, 20, 35))
    draw.rectangle([0, 60, 63, 63], fill=(30, 20, 35))
    draw.line([(31, 0), (31, 63)], fill=(30, 20, 35), width=2)
    draw.line([(0, 31), (63, 31)], fill=(30, 20, 35), width=2)
    return im

def make_wistful_2():
    # Retro cassette tape with glowing sunset horizon
    im = make_gradient((60, 30, 80), (230, 130, 110))
    draw = ImageDraw.Draw(im)
    # Cassette silhouette
    draw.rectangle([10, 20, 53, 48], fill=(35, 20, 40), outline=(230, 180, 140))
    draw.rectangle([16, 26, 47, 42], fill=(220, 190, 150))
    draw.ellipse([21, 30, 29, 38], fill=(35, 20, 40))
    draw.ellipse([34, 30, 42, 38], fill=(35, 20, 40))
    # Ribbon window
    draw.rectangle([28, 32, 35, 36], fill=(50, 25, 45))
    return im

# --- 5. LONGING 1 & 2 ---
def make_longing_1():
    # Lonely cliff silhouette gazing at distant lighthouse across dark indigo sea
    im = make_gradient((12, 10, 30), (20, 30, 65))
    pixels = im.load()
    draw_stars(im, count=30, rng=random.Random(33))
    draw = ImageDraw.Draw(im)
    # Ocean horizon
    draw.rectangle([0, 38, 63, 63], fill=(10, 22, 50))
    # Sea waves dithering
    for y in range(40, 64, 3):
        for x in range(0, 64, 4):
            pixels[(x + (y*2)) % 64, y] = (35, 60, 110)
    # Distant lighthouse
    draw.rectangle([52, 28, 55, 38], fill=(220, 220, 240))
    draw.polygon([(51, 28), (56, 28), (53, 25)], fill=(180, 50, 50))
    # Lighthouse beam
    for by in range(24, 38):
        beam_len = (38 - by) * 4
        for bx in range(max(0, 52 - beam_len), 52):
            pixels[bx, by] = lerp_rgb(pixels[bx, by], (255, 255, 200), 0.4)
    # Foreground cliff silhouette with figure
    draw.polygon([(0, 28), (18, 34), (22, 63), (0, 63)], fill=(8, 6, 16))
    # Person silhouette
    pixels[16, 26] = (8, 6, 16) # head
    draw.rectangle([15, 27, 17, 34], fill=(8, 6, 16)) # body
    return im

def make_longing_2():
    # Distant glowing window in twilight rain, reaching out
    im = make_gradient((20, 15, 45), (45, 35, 80))
    pixels = im.load()
    draw_stars(im, count=15, rng=random.Random(44))
    draw = ImageDraw.Draw(im)
    # Distant building with warm glowing yellow window
    draw.rectangle([24, 20, 40, 44], fill=(12, 10, 25))
    draw.rectangle([28, 25, 36, 35], fill=(255, 215, 100))
    draw.line([(32, 25), (32, 35)], fill=(50, 35, 15), width=1)
    draw.line([(28, 30), (36, 30)], fill=(50, 35, 15), width=1)
    # Warm glow around window
    for rx in range(22, 43):
        for ry in range(19, 41):
            if rx < 28 or rx > 36 or ry < 25 or ry > 35:
                d = math.sqrt((rx - 32)**2 + (ry - 30)**2)
                if d < 12:
                    pixels[rx, ry] = lerp_rgb(pixels[rx, ry], (255, 220, 120), (1 - d/12.0)*0.4)
    return im

# --- 6. IRONIC 1 & 2 ---
def make_ironic_1():
    # Neon smiling masquerade mask with acid lime and magenta glitches
    im = make_gradient((10, 5, 20), (30, 8, 40))
    pixels = im.load()
    draw = ImageDraw.Draw(im)
    # Neon grid on bottom
    for x in range(0, 64, 8):
        draw.line([(x, 48), (x + (x-32), 63)], fill=(80, 240, 90, 100))
    draw.line([(0, 52), (63, 52)], fill=(50, 180, 70))
    draw.line([(0, 57), (63, 57)], fill=(50, 180, 70))
    # Smiling mask outline in bright neon lime
    draw.ellipse([18, 16, 45, 40], outline=(100, 255, 110), width=2)
    # Angled eye slits
    draw.line([(23, 24), (28, 26)], fill=(255, 50, 150), width=2)
    draw.line([(40, 24), (35, 26)], fill=(255, 50, 150), width=2)
    # Exaggerated cynical smirk
    draw.arc([22, 26, 41, 36], start=20, end=160, fill=(100, 255, 110), width=2)
    draw.line([(40, 29), (43, 27)], fill=(100, 255, 110), width=2) # smirk lift
    # Glitch lines
    for gx in range(12, 52, 2):
        if gx % 6 == 0:
            pixels[gx, 22] = (255, 50, 180)
            pixels[gx, 34] = (80, 255, 230)
    return im

def make_ironic_2():
    # Neon tongue-in-cheek wink emoji marquee
    im = make_gradient((5, 12, 10), (20, 45, 30))
    draw = ImageDraw.Draw(im)
    # Marquee border
    draw.rectangle([12, 12, 51, 51], outline=(50, 255, 120), width=2)
    # Winking face
    draw.ellipse([18, 18, 45, 45], outline=(255, 230, 60), width=2)
    # Left eye open
    draw.ellipse([24, 26, 28, 30], fill=(50, 255, 120))
    # Right eye winking line
    draw.line([(35, 28), (40, 28)], fill=(255, 80, 180), width=2)
    # Sarcastic smile
    draw.arc([24, 30, 39, 40], start=10, end=170, fill=(255, 230, 60), width=2)
    return im

# --- 7. ADDITIONAL VARIATIONS (2.png) FOR ALL MOODS ---
def make_happy_2():
    # Tropical sunburst with palm tree silhouette
    im = make_gradient((255, 120, 60), (255, 220, 80), curve=1.2)
    draw = ImageDraw.Draw(im)
    draw.ellipse([20, 12, 44, 36], fill=(255, 255, 200)) # glowing sun
    # Sea
    draw.rectangle([0, 44, 63, 63], fill=(20, 140, 190))
    # Island & palm silhouette
    draw.polygon([(0, 63), (25, 52), (40, 63)], fill=(20, 40, 30))
    # Trunk
    for y in range(30, 54):
        x = 22 + int(math.sin((y - 30) * 0.1) * 4)
        im.putpixel((x, y), (20, 40, 30))
        im.putpixel((x+1, y), (20, 40, 30))
    # Palm fronds
    draw.line([(22, 30), (8, 26)], fill=(25, 60, 40), width=2)
    draw.line([(22, 30), (36, 24)], fill=(25, 60, 40), width=2)
    draw.line([(22, 30), (14, 38)], fill=(25, 60, 40), width=2)
    draw.line([(22, 30), (32, 36)], fill=(25, 60, 40), width=2)
    return im

def make_sad_2():
    # Cold rain streaks on dark blue city street lamp
    im = make_gradient((5, 12, 30), (15, 35, 65))
    pixels = im.load()
    # Rain streaks
    r = random.Random(99)
    for _ in range(60):
        rx = r.randint(0, 60)
        ry = r.randint(0, 55)
        length = r.randint(3, 7)
        for step in range(length):
            if ry + step < 64:
                pixels[rx, ry + step] = (90, 130, 180)
    draw = ImageDraw.Draw(im)
    # Solitary street lamp
    draw.line([(45, 20), (45, 63)], fill=(30, 40, 60), width=2)
    draw.ellipse([40, 15, 50, 22], fill=(220, 230, 255))
    # Lamp reflection in puddle
    draw.ellipse([30, 56, 58, 62], fill=(40, 70, 110))
    return im

def make_angry_2():
    # Volcanic fiery fissure with smoke and molten lava
    im = make_gradient((20, 5, 8), (140, 20, 15))
    draw = ImageDraw.Draw(im)
    # Magma jagged crack
    crack = [(0, 50), (14, 45), (25, 52), (40, 42), (52, 48), (63, 40)]
    draw.line(crack, fill=(255, 220, 50), width=3)
    draw.line(crack, fill=(255, 70, 20), width=1)
    # Lava pools
    draw.ellipse([18, 54, 38, 62], fill=(255, 120, 20))
    draw.ellipse([22, 56, 34, 60], fill=(255, 240, 80))
    pixels = im.load()
    r = random.Random(777)
    for _ in range(35):
        pixels[r.randint(5, 58), r.randint(10, 45)] = (255, r.randint(100, 220), 40)
    return im

def make_tender_2():
    # Twilight lavender meadow with soft glowing fireflies
    im = make_gradient((50, 30, 70), (180, 140, 190))
    draw = ImageDraw.Draw(im)
    # Gentle grassy hills
    draw.polygon([(0, 48), (30, 42), (63, 46), (63, 63), (0, 63)], fill=(55, 45, 75))
    draw.polygon([(0, 54), (45, 50), (63, 56), (63, 63), (0, 63)], fill=(40, 32, 60))
    pixels = im.load()
    # Fireflies
    r = random.Random(321)
    for _ in range(20):
        fx = r.randint(4, 59)
        fy = r.randint(15, 55)
        pixels[fx, fy] = (255, 255, 180)
        if 0 < fx < 63 and 0 < fy < 63:
            pixels[fx+1, fy] = (200, 180, 120)
            pixels[fx, fy+1] = (200, 180, 120)
    return im

def make_cynical_2():
    # Eerie surveillance CRT monitor with green glitch scanlines
    im = make_gradient((5, 15, 8), (15, 40, 20))
    pixels = im.load()
    # CRT scanlines
    for y in range(0, 64, 2):
        for x in range(64):
            pixels[x, y] = lerp_rgb(pixels[x, y], (0, 0, 0), 0.4)
    draw = ImageDraw.Draw(im)
    # Security camera silhouette in corner
    draw.polygon([(48, 8), (62, 16), (58, 22), (44, 14)], fill=(120, 255, 140))
    draw.line([(63, 10), (52, 14)], fill=(80, 200, 100), width=2)
    # Blinking REC dot
    draw.ellipse([8, 8, 12, 12], fill=(255, 30, 30))
    draw.text((16, 6), "REC", fill=(120, 255, 140))
    return im

def make_neutral_2():
    # Minimalist balanced geometric zen stones under mist
    im = make_gradient((45, 50, 55), (90, 95, 100))
    draw = ImageDraw.Draw(im)
    # Balanced cairn stones
    draw.ellipse([22, 50, 42, 60], fill=(30, 32, 35))
    draw.ellipse([24, 42, 40, 50], fill=(50, 54, 58))
    draw.ellipse([27, 35, 37, 42], fill=(70, 75, 80))
    draw.ellipse([29, 30, 35, 35], fill=(95, 100, 105))
    # Calming water ripples
    draw.arc([14, 56, 50, 63], start=0, end=180, fill=(130, 135, 140))
    return im

def make_festive_2():
    # Carnival night with fireworks over sparkling fairground
    im = make_gradient((15, 8, 35), (45, 20, 60))
    pixels = im.load()
    draw_stars(im, count=20, rng=random.Random(555))
    # Fireworks burst
    cx, cy = 32, 22
    r = random.Random(888)
    burst_colors = [(255, 80, 120), (255, 230, 70), (80, 240, 255), (160, 255, 100)]
    for _ in range(60):
        angle = r.random() * math.pi * 2
        rad = r.randint(4, 18)
        px = clamp(cx + math.cos(angle) * rad)
        py = clamp(cy + math.sin(angle) * rad)
        pixels[px, py] = r.choice(burst_colors)
    draw = ImageDraw.Draw(im)
    # Fairground silhouettes
    draw.polygon([(0, 63), (12, 50), (24, 63)], fill=(20, 10, 30))
    draw.polygon([(40, 63), (52, 48), (63, 63)], fill=(20, 10, 30))
    # Ferris wheel rim
    draw.ellipse([18, 38, 46, 66], outline=(255, 210, 80), width=1)
    return im

def make_dreamy_2():
    # Ringed planet rising over purple crystal sea
    im = make_gradient((25, 10, 45), (80, 40, 100))
    pixels = im.load()
    draw_stars(im, count=35, rng=random.Random(919))
    draw = ImageDraw.Draw(im)
    # Giant pastel planet
    draw.ellipse([20, 12, 44, 36], fill=(240, 190, 220))
    # Planet ring
    draw.arc([10, 20, 54, 28], start=0, end=180, fill=(200, 240, 255), width=2)
    # Shimmering crystal sea
    draw.rectangle([0, 46, 63, 63], fill=(45, 20, 70))
    for y in range(48, 64, 2):
        for x in range(0, 64, 6):
            pixels[(x + y) % 64, y] = (180, 150, 240)
    return im

def make_romantic_2():
    # Fairy-lit balcony with warm amber city lights bokeh
    im = make_gradient((30, 12, 28), (120, 50, 70))
    draw = ImageDraw.Draw(im)
    # City bokeh orbs
    r = random.Random(214)
    for _ in range(16):
        bx = r.randint(5, 58)
        by = r.randint(18, 48)
        rad = r.randint(2, 5)
        col = r.choice([(255, 180, 140), (255, 120, 150), (255, 220, 180)])
        draw.ellipse([bx - rad, by - rad, bx + rad, by + rad], fill=col)
    # Balcony string lights
    draw.arc([4, 6, 60, 22], start=20, end=160, fill=(255, 240, 180), width=1)
    for lx in [12, 22, 32, 42, 52]:
        draw.ellipse([lx-1, 14, lx+1, 17], fill=(255, 255, 200))
    draw.rectangle([0, 52, 63, 63], fill=(20, 8, 16))
    return im

def make_melancholic_2():
    # Autumn park bench under a lone weeping willow beside foggy water
    im = make_gradient((10, 20, 40), (40, 65, 95))
    pixels = im.load()
    # Mist lines
    for y in range(32, 48, 3):
        for x in range(64):
            dt = dither_val(0.3, x, y)
            pixels[x, y] = lerp_rgb(pixels[x, y], (140, 170, 200), dt)
    draw = ImageDraw.Draw(im)
    # Ground
    draw.polygon([(0, 48), (63, 44), (63, 63), (0, 63)], fill=(15, 25, 45))
    # Park bench silhouette
    draw.rectangle([18, 46, 36, 49], fill=(10, 16, 28))
    draw.line([(21, 49), (21, 55)], fill=(10, 16, 28), width=2)
    draw.line([(33, 49), (33, 55)], fill=(10, 16, 28), width=2)
    draw.line([(18, 42), (36, 42)], fill=(10, 16, 28), width=2)
    # Willow branches
    for bx in range(40, 64, 4):
        draw.line([(55, 8), (bx, 38)], fill=(18, 32, 50), width=1)
    return im

def make_frustrated_2():
    # Broken neon sign flickering in pouring rain
    im = make_gradient((15, 8, 10), (50, 18, 12))
    pixels = im.load()
    r = random.Random(404)
    # Rain
    for _ in range(50):
        rx = r.randint(0, 63)
        ry = r.randint(0, 55)
        for s in range(r.randint(2, 5)):
            if ry + s < 64:
                pixels[rx, ry + s] = (100, 60, 50)
    draw = ImageDraw.Draw(im)
    # Broken neon lightning bar
    draw.line([(10, 25), (26, 25), (20, 36), (38, 36), (32, 48), (48, 48)], fill=(255, 140, 30), width=2)
    # Sparks falling
    for sx, sy in [(22, 38), (28, 42), (34, 52), (40, 55)]:
        pixels[sx, sy] = (255, 240, 160)
    return im

def make_energetic_2():
    # Cyberpunk high-speed neon sound tunnel
    im = Image.new("RGB", (64, 64))
    pixels = im.load()
    for y in range(64):
        for x in range(64):
            dx = x - 32
            dy = y - 32
            dist = math.sqrt(dx*dx + dy*dy)
            ring = int(dist * 0.4) % 2
            col = (255, 190, 30) if ring else (30, 200, 255)
            fade = max(0.0, min(1.0, dist / 32.0))
            pixels[x, y] = lerp_rgb((10, 5, 20), col, fade)
    # Speed lines
    draw = ImageDraw.Draw(im)
    for angle in [0.3, 0.9, 1.6, 2.4, 3.5, 4.3, 5.2]:
        x2 = int(32 + math.cos(angle) * 32)
        y2 = int(32 + math.sin(angle) * 32)
        draw.line([(32, 32), (x2, y2)], fill=(255, 255, 255), width=1)
    return im

def make_calm_2():
    # Morning mist over tranquil mountain lake and pines
    im = make_gradient((50, 85, 110), (140, 185, 175))
    pixels = im.load()
    draw = ImageDraw.Draw(im)
    # Calm lake reflection
    draw.rectangle([0, 42, 63, 63], fill=(60, 110, 115))
    # Pine trees silhouette
    for tx in [8, 16, 28, 48, 56]:
        th = 18 if tx % 2 == 0 else 24
        draw.polygon([(tx, 42 - th), (tx - 5, 42), (tx + 5, 42)], fill=(25, 45, 40))
    # Soft mist band
    for y in range(38, 46):
        for x in range(64):
            pixels[x, y] = lerp_rgb(pixels[x, y], (200, 230, 220), 0.4)
    return im

def make_tense_2():
    # Suspenseful ticking clock half-shrouded in shadow
    im = make_gradient((8, 6, 12), (25, 20, 15))
    draw = ImageDraw.Draw(im)
    # Clock face circle
    draw.ellipse([14, 14, 50, 50], outline=(180, 140, 60), width=2)
    # Clock hands at 11:59
    draw.line([(32, 32), (30, 18)], fill=(255, 210, 80), width=2)
    draw.line([(32, 32), (32, 16)], fill=(255, 50, 40), width=1)
    # Shadow dividing clock in half
    pixels = im.load()
    for y in range(64):
        for x in range(32, 64):
            pixels[x, y] = lerp_rgb(pixels[x, y], (0, 0, 0), 0.6)
    return im

def make_bittersweet_2():
    # Dandelion seeds floating from sunlit grass into evening sky
    im = make_gradient((40, 30, 70), (220, 140, 90))
    draw = ImageDraw.Draw(im)
    draw.ellipse([42, 16, 56, 30], fill=(255, 240, 180)) # soft sunset sun
    draw.polygon([(0, 52), (63, 46), (63, 63), (0, 63)], fill=(45, 30, 25))
    pixels = im.load()
    # Floating seeds
    r = random.Random(111)
    for _ in range(25):
        sx = r.randint(10, 58)
        sy = r.randint(12, 50)
        pixels[sx, sy] = (255, 255, 240)
        if 0 < sx < 63 and 0 < sy < 63:
            pixels[sx, sy-1] = (200, 190, 180)
            pixels[sx+1, sy-1] = (200, 190, 180)
    return im

def make_hopeful_2():
    # Paper lantern floating into dawn starlight over quiet sea
    im = make_gradient((20, 25, 60), (90, 140, 190))
    draw_stars(im, count=25, rng=random.Random(707))
    draw = ImageDraw.Draw(im)
    # Sea
    draw.rectangle([0, 48, 63, 63], fill=(15, 35, 65))
    # Glowing lantern
    draw.rectangle([28, 22, 36, 32], fill=(255, 200, 60))
    draw.polygon([(26, 22), (38, 22), (32, 19)], fill=(220, 140, 30))
    # Glow
    pixels = im.load()
    for rx in range(22, 43):
        for ry in range(16, 38):
            d = math.sqrt((rx - 32)**2 + (ry - 27)**2)
            if d < 10 and (rx < 28 or rx > 36 or ry < 22 or ry > 32):
                pixels[rx, ry] = lerp_rgb(pixels[rx, ry], (255, 220, 100), (1 - d/10.0)*0.5)
    return im

def make_cheerful_2():
    # Hot air balloons floating over sunny patchwork fields
    im = make_gradient((80, 170, 240), (200, 230, 255))
    draw = ImageDraw.Draw(im)
    # Patchwork hills
    draw.polygon([(0, 48), (25, 42), (48, 45), (63, 40), (63, 63), (0, 63)], fill=(120, 200, 60))
    draw.polygon([(0, 54), (32, 48), (63, 52), (63, 63), (0, 63)], fill=(80, 170, 40))
    # Balloon 1
    draw.ellipse([18, 14, 30, 26], fill=(255, 70, 80))
    draw.polygon([(21, 26), (27, 26), (24, 28)], fill=(200, 40, 50))
    draw.rectangle([23, 29, 25, 31], fill=(140, 80, 40))
    # Balloon 2 (small)
    draw.ellipse([42, 22, 50, 30], fill=(255, 200, 40))
    return im

def make_defiant_2():
    # Lightning striking a mountaintop tower with waving neon banner
    im = make_gradient((10, 5, 25), (40, 15, 30))
    draw = ImageDraw.Draw(im)
    # Mountain peak
    draw.polygon([(0, 63), (32, 28), (63, 63)], fill=(18, 12, 25))
    # Tower
    draw.rectangle([30, 20, 34, 28], fill=(200, 180, 200))
    # Waving flag banner
    draw.polygon([(34, 20), (46, 23), (34, 26)], fill=(255, 40, 70))
    # Lightning bolt striking tower
    draw.line([(24, 0), (28, 12), (25, 12), (32, 20)], fill=(255, 255, 200), width=2)
    return im

def make_blue_2():
    # Neon 'BLUES' glowing on wet cobblestones in late night rain
    im = make_gradient((8, 12, 35), (20, 35, 75))
    pixels = im.load()
    draw = ImageDraw.Draw(im)
    # Brick wall background
    for y in range(0, 45, 6):
        draw.line([(0, y), (63, y)], fill=(15, 20, 45), width=1)
    # Wet street ground
    draw.rectangle([0, 45, 63, 63], fill=(12, 18, 40))
    # Neon sign glowing blue
    draw.rectangle([14, 18, 50, 32], outline=(40, 140, 255), width=2)
    draw.text((18, 20), "BLUES", fill=(120, 210, 255))
    # Blue reflection in street puddle
    draw.ellipse([18, 50, 46, 58], fill=(30, 90, 180))
    return im

GENERATORS = {
    # Missing 1.png
    ("uplifting", "1.png"): make_uplifting_1,
    ("uplifting", "2.png"): make_uplifting_2,
    ("intense", "1.png"): make_intense_1,
    ("intense", "2.png"): make_intense_2,
    ("dramatic", "1.png"): make_dramatic_1,
    ("dramatic", "2.png"): make_dramatic_2,
    ("wistful", "1.png"): make_wistful_1,
    ("wistful", "2.png"): make_wistful_2,
    ("longing", "1.png"): make_longing_1,
    ("longing", "2.png"): make_longing_2,
    ("ironic", "1.png"): make_ironic_1,
    ("ironic", "2.png"): make_ironic_2,
    # 2.png for all others
    ("happy", "2.png"): make_happy_2,
    ("sad", "2.png"): make_sad_2,
    ("angry", "2.png"): make_angry_2,
    ("tender", "2.png"): make_tender_2,
    ("cynical", "2.png"): make_cynical_2,
    ("neutral", "2.png"): make_neutral_2,
    ("festive", "2.png"): make_festive_2,
    ("dreamy", "2.png"): make_dreamy_2,
    ("romantic", "2.png"): make_romantic_2,
    ("melancholic", "2.png"): make_melancholic_2,
    ("frustrated", "2.png"): make_frustrated_2,
    ("energetic", "2.png"): make_energetic_2,
    ("calm", "2.png"): make_calm_2,
    ("tense", "2.png"): make_tense_2,
    ("bittersweet", "2.png"): make_bittersweet_2,
    ("hopeful", "2.png"): make_hopeful_2,
    ("cheerful", "2.png"): make_cheerful_2,
    ("defiant", "2.png"): make_defiant_2,
    ("blue", "2.png"): make_blue_2,
}

base_dir = "/home/tina/karaoke/src/karaoke/static/images/moods"
for (mood, filename), gen_fn in GENERATORS.items():
    mdir = os.path.join(base_dir, mood)
    os.makedirs(mdir, exist_ok=True)
    dst = os.path.join(mdir, filename)
    img = gen_fn()
    img.save(dst, "PNG")
    print(f"Generated {dst}")
