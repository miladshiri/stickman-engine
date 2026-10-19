
from stickman_engine import *

# ============================================================
# HOW TO MAKE PASSIVE INCOME
# 30-second educational stickman animation
# ============================================================

DURATION = 30.0
FPS = 30

scene = Scene(
    duration=DURATION,
    fps=FPS,
    background="grid",
    sound=True,
    sfx_volume=0.8,
)

# Main character
bob = scene.add_character(
    "bob",
    x=0.34,
    ground=0.86,
    scale=1.0,
    color=INK,
    facing=1,
)

# Upbeat background music
scene.music(
    style="upbeat",
    volume=0.25,
    t0=0,
    t1=DURATION,
)

scene.fade_in(0.35)

# ------------------------------------------------------------
# BEAT 1 — HOOK (0–3 seconds)
# ------------------------------------------------------------

scene.title(
    0.25, 2.9,
    "PASSIVE INCOME",
    sub="Make money beyond your 9-to-5",
    size=100,
    color=INK,
    y=0.30,
)

bob.pose(0.25, "wave", dur=0.35, ease="back", face="happy")
bob.layer("wave", 0.35, 1.35)
bob.pose(1.5, "point_you", dur=0.3, ease="back", face="excited")
bob.layer("talk", 1.6, 2.7)

scene.caption(
    2.0, 3.0,
    "3 IDEAS TO GET STARTED",
    pos="bottom",
    size=46,
    color=BLUE,
)

# ------------------------------------------------------------
# BEAT 2 — WHAT IT MEANS (3–7 seconds)
# ------------------------------------------------------------

bob.pose(3.0, "think", dur=0.35, face="thinking")
bob.layer("look_around", 3.3, 4.0)

scene.prop(
    "lightbulb",
    3.6, 6.8,
    at="bob",
    anchor="above_head",
    offset=(0.35, -0.05),
    scale=1.0,
    anim="pop",
)

bob.pose(4.3, "point_up", dur=0.35, ease="back", face="excited")
bob.layer("nod", 4.8, 6.5)

scene.speech(
    "bob", 3.35, 6.8,
    "Build something that can earn over time!",
    kind="speech",
    side=1,
    size=36,
    voice=True,
    voice_pitch=1.05,
)

scene.caption(
    5.7, 7.0,
    "WORK UPFRONT. POTENTIAL REWARDS LATER.",
    pos="bottom",
    size=34,
    color=INK,
)

# ------------------------------------------------------------
# BEAT 3 — IDEA 1: DIGITAL PRODUCTS (7–12 seconds)
# ------------------------------------------------------------

bob.pose(7.0, "present", dur=0.35, face="happy")
bob.layer("talk", 7.5, 9.0)

scene.caption(
    7.1, 11.9,
    "1. SELL DIGITAL PRODUCTS",
    pos="top",
    size=48,
    color=BLUE,
)

scene.prop(
    "laptop",
    7.2, 11.9,
    x=0.73, y=0.62,
    scale=1.15,
    anim="pop",
)

scene.caption(
    8.6, 11.9,
    "Templates • E-books • Online guides",
    pos=(0.73, 0.78),
    size=31,
    color=INK,
)

bob.pose(9.2, "type", dur=0.35, face="focused")
bob.layer("type", 9.5, 11.3)

scene.effect(
    "sparkles",
    10.8, 11.7,
    x=0.76, y=0.49,
    count=7,
    color=GOLD,
    intensity=0.8,
)

# ------------------------------------------------------------
# BEAT 4 — IDEA 2: INVESTING (12–17 seconds)
# ------------------------------------------------------------

bob.pose(12.0, "think", dur=0.35, face="thinking")
bob.pose(12.8, "point_side", dur=0.3, ease="back", face="happy")

scene.caption(
    12.0, 16.9,
    "2. INVEST FOR POTENTIAL RETURNS",
    pos="top",
    size=43,
    color=GREEN,
)

# FIX: bars must be a collection, not an integer.
scene.prop(
    "chart",
    12.1, 16.8,
    x=0.74, y=0.53,
    scale=1.2,
    anim="grow",
    bars=[0.25, 0.4, 0.55, 0.75, 1.0],
    grow=1.0,
)

scene.prop(
    "coin_stack",
    13.3, 16.7,
    x=0.83, y=0.76,
    scale=0.9,
    anim="pop",
)

scene.caption(
    14.0, 16.9,
    "Dividends • Long-term growth",
    pos="bottom",
    size=36,
    color=INK,
)

scene.effect(
    "sparkles",
    15.0, 16.4,
    x=0.79, y=0.40,
    count=8,
    color=GOLD,
    intensity=0.8,
)

bob.pose(15.1, "present", dur=0.35, face="proud")
bob.layer("nod", 15.5, 16.7)

# ------------------------------------------------------------
# BEAT 5 — IDEA 3: ROYALTIES (17–22 seconds)
# ------------------------------------------------------------

bob.pose(17.0, "read", dur=0.35, face="focused")

scene.caption(
    17.0, 21.9,
    "3. EARN ROYALTIES",
    pos="top",
    size=52,
    color=PURPLE,
)

scene.prop(
    "book",
    17.1, 21.8,
    x=0.73, y=0.58,
    scale=1.25,
    anim="pop",
    color=BLUE,
    title="YOUR IDEAS",
)

scene.caption(
    18.4, 21.9,
    "Books • Music • Creative work",
    pos="bottom",
    size=37,
    color=INK,
)

bob.pose(19.1, "present", dur=0.35, face="happy")
bob.layer("talk", 19.5, 21.2)

scene.effect(
    "sparkles",
    20.4, 21.6,
    x=0.79, y=0.42,
    count=7,
    color=PURPLE,
    intensity=0.9,
)

# ------------------------------------------------------------
# BEAT 6 — RECAP (22–26 seconds)
# ------------------------------------------------------------

bob.pose(22.0, "point_side", dur=0.3, face="excited")

scene.caption(
    22.0, 25.9,
    "CREATE. INVEST. EARN ROYALTIES.",
    pos="top",
    size=44,
    color=INK,
)

scene.prop(
    "sign",
    22.2, 25.8,
    x=0.73, y=0.49,
    scale=1.05,
    anim="pop",
    text="BUILD ASSETS",
)

bob.pose(23.4, "cheer", dur=0.35, ease="back", face="very_happy")
bob.layer("bounce", 23.8, 25.4)

scene.effect(
    "confetti",
    23.5, 25.2,
    x=0.52, y=0.43,
    count=24,
    intensity=0.8,
)

scene.caption(
    24.1, 25.9,
    "Small steps can build over time.",
    pos="bottom",
    size=34,
    color=GREEN,
)

# ------------------------------------------------------------
# BEAT 7 — REALISTIC ENDING (26–30 seconds)
# ------------------------------------------------------------

bob.pose(26.0, "think", dur=0.4, face="thinking")
bob.layer("nod", 26.4, 27.1)

scene.caption(
    26.0, 29.7,
    "PASSIVE INCOME TAKES WORK UPFRONT.",
    pos="top",
    size=43,
    color=INK,
)

scene.speech(
    "bob", 26.3, 29.1,
    "Build it patiently. Keep improving it!",
    kind="speech",
    side=1,
    size=34,
    voice=True,
    voice_pitch=1.0,
)

bob.pose(28.0, "point_you", dur=0.35, ease="back", face="happy")
bob.layer("nod", 28.4, 29.5)

scene.caption(
    28.2, 29.7,
    "START SMALL. STAY CONSISTENT.",
    pos="bottom",
    size=36,
    color=GREEN,
)

# Fade out and render
scene.fade_out(0.5)
scene.render("passive_income.mp4")
