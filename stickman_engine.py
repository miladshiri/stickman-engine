#!/usr/bin/env python3
"""
stickman_engine.py - single-file hand-drawn stickman animation engine.

    pip3 install numpy pillow imageio imageio-ffmpeg

Write a scene script next to this file:

    from stickman_engine import *
    scene = Scene(duration=6)
    bob = scene.add_character("bob", x=0.3)
    bob.pose(0.3, "wave", dur=0.4, ease="back", face="happy")
    bob.layer("wave", 0.5, 1.8)
    bob.walk_to(2.0, 0.6)
    scene.caption(0.2, 2.0, "Hello!")
    scene.render()          # CLI: --preview  --frame N  --out file.mp4  --ss 3  --no-sound  --audio-only

Design space is always 1920x1080 (x right, y down). Times are seconds (floats).
Sound effects are synthesized procedurally (no audio files needed) and muxed into the MP4 with ffmpeg.
"""
import argparse
import math
import os
import random
import subprocess
import sys
import textwrap
import wave
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# =============================================================== constants
BW, BH = 1920, 1080
INK = (17, 17, 17)
PAPER = (255, 255, 255)
GRID = (212, 223, 236)
HC = 430.0                       # default character height in design px
SW = 0.018 * HC                  # default stroke width

GREEN = (40, 140, 60)
LIGHT_GREEN = (196, 232, 180)
GOLD = (255, 212, 95)
YELLOW = (255, 232, 120)
RED = (220, 60, 60)
PINK = (255, 120, 150)
BLUE = (70, 130, 220)
LIGHT_BLUE = (185, 215, 255)
GRAY = (150, 150, 150)
LIGHT_GRAY = (235, 238, 244)
ORANGE = (255, 160, 60)
PURPLE = (150, 90, 210)


# =============================================================== math helpers
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def lerp(a, b, u):
    return a + (b - a) * u


def window(t, a, b):
    """0 before a, 1 after b, linear between."""
    if b <= a:
        return 1.0 if t >= a else 0.0
    return clamp((t - a) / (b - a))


def ease_linear(u):
    return clamp(u)


def ease_in(u):
    return clamp(u) ** 3


def ease_out(u):
    return 1 - (1 - clamp(u)) ** 3


def ease_io(u):
    u = clamp(u)
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def ease_back(u, s=1.9):
    u = clamp(u) - 1
    return 1 + (s + 1) * u ** 3 + s * u ** 2


def ease_elastic(u):
    u = clamp(u)
    if u in (0.0, 1.0):
        return u
    return 2 ** (-10 * u) * math.sin((u * 10 - 0.75) * (2 * math.pi / 3)) + 1


def ease_bounce(u):
    u = clamp(u)
    n, d = 7.5625, 2.75
    if u < 1 / d:
        return n * u * u
    if u < 2 / d:
        u -= 1.5 / d
        return n * u * u + 0.75
    if u < 2.5 / d:
        u -= 2.25 / d
        return n * u * u + 0.9375
    u -= 2.625 / d
    return n * u * u + 0.984375


EASES = {"linear": ease_linear, "in": ease_in, "out": ease_out, "io": ease_io,
         "back": ease_back, "elastic": ease_elastic, "bounce": ease_bounce}


def pop_scale(t, t0, t1, din=0.35, dout=0.2):
    """Scale factor for things that pop in at t0 and pop out at t1 (elastic in, ease-in out)."""
    if t < t0 or t > t1:
        return 0.0
    return ease_elastic(window(t, t0, t0 + din)) * (1 - ease_in(window(t, t1 - dout, t1)))


def rot(v, a):
    c, s = math.cos(a), math.sin(a)
    return np.array([v[0] * c - v[1] * s, v[0] * s + v[1] * c])


def V(x, y):
    return np.array([float(x), float(y)])


def sd(name):
    return zlib.crc32(str(name).encode())


def catmull(pts, n=10, closed=False):
    P = np.asarray(pts, float)
    if len(P) < 2:
        return P
    if len(P) == 2 and not closed:
        return np.linspace(P[0], P[1], n)
    if closed:
        P = np.vstack([P[-1], P, P[0], P[1]])
    else:
        P = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
    ts = np.linspace(0, 1, n, endpoint=False)[:, None]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        out.append(0.5 * (2 * p1 + (-p0 + p2) * ts + (2 * p0 - 5 * p1 + 4 * p2 - p3) * ts ** 2
                          + (-p0 + 3 * p1 - 3 * p2 + p3) * ts ** 3))
    if not closed:
        out.append(P[-2][None])
    return np.vstack(out)


def noodle(A, M, B, n=18):
    """Quadratic Bezier A->B passing through M (soft curvy limb)."""
    A, M, B = np.asarray(A, float), np.asarray(M, float), np.asarray(B, float)
    C = 2 * M - (A + B) / 2
    u = np.linspace(0, 1, n)[:, None]
    return (1 - u) ** 2 * A + 2 * (1 - u) * u * C + u ** 2 * B


def ellipse_pts(c, rx, ry, ang=0.0, n=40):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    x, y = rx * np.cos(a), ry * np.sin(a)
    ca, sa = math.cos(ang), math.sin(ang)
    return np.stack([c[0] + x * ca - y * sa, c[1] + x * sa + y * ca], 1)


def wobble(P, seed, amp=1.3, closed=False):
    """Stable hand-drawn wobble along arc length (fixed seed -> no flicker)."""
    if amp <= 0 or len(P) < 3:
        return P
    r = random.Random(seed)
    d = np.gradient(P, axis=0)
    nrm = np.stack([-d[:, 1], d[:, 0]], 1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9
    s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))])
    f1, f2 = r.uniform(0.02, 0.04), r.uniform(0.06, 0.1)
    p1, p2 = r.uniform(0, 6.28), r.uniform(0, 6.28)
    off = amp * (0.7 * np.sin(s * f1 + p1) + 0.3 * np.sin(s * f2 + p2))
    if closed and s[-1] > 0:
        off *= np.sin(np.pi * s / s[-1])
    return P + nrm * off[:, None]


def ik2(root, target, l1, l2, prefer):
    """Two-bone IK. prefer = 2D vector; the joint bends toward the side that best matches it."""
    root, target = np.asarray(root, float), np.asarray(target, float)
    v = target - root
    n = np.linalg.norm(v) + 1e-9
    dv = v / n
    d = clamp(n, abs(l1 - l2) + 1e-3, l1 + l2 - 1e-3)
    a = (l1 * l1 - l2 * l2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, l1 * l1 - a * a))
    perp = np.array([-dv[1], dv[0]])
    if perp @ np.asarray(prefer, float) < 0:
        perp = -perp
    return root + dv * a + perp * h, root + dv * d


# =============================================================== fonts
_FONT_PATHS = ["/System/Library/Fonts/MarkerFelt.ttc",
               "/System/Library/Fonts/Supplemental/Chalkboard.ttc",
               "/System/Library/Fonts/Supplemental/Comic Sans MS.ttf",
               "C:/Windows/Fonts/comic.ttf",
               "/usr/share/fonts/truetype/comic-neue/ComicNeue-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_FONT_CACHE = {}


def get_font(px):
    px = max(6, int(px))
    if px in _FONT_CACHE:
        return _FONT_CACHE[px]
    f = None
    for p in _FONT_PATHS:
        try:
            f = ImageFont.truetype(p, px)
            break
        except Exception:
            pass
    if f is None:
        try:
            f = ImageFont.load_default(size=px)
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[px] = f
    return f


# =============================================================== canvas
class Canvas:
    """Drawing surface. All coordinates/sizes are in 1920x1080 design units.
    World drawing goes through the camera; set cv.screen=True for HUD drawing."""

    def __init__(self, img, k, cam=(BW / 2, BH / 2, 1.0)):
        self.img, self.k, self.cam = img, k, cam
        self.d = ImageDraw.Draw(img)
        self.screen = False

    # --- transforms
    def zoom(self):
        return 1.0 if self.screen else self.cam[2]

    def tp(self, x, y):
        if self.screen:
            return x * self.k, y * self.k
        cx, cy, z = self.cam
        return ((x - cx) * z + BW / 2) * self.k, ((y - cy) * z + BH / 2) * self.k

    def _p(self, P):
        return [self.tp(float(x), float(y)) for x, y in P]

    def _w(self, w):
        return max(1, int(round(w * self.k * self.zoom())))

    # --- primitives
    def stroke(self, P, w=SW, color=INK, closed=False, fill=None, wob=1.3, seed=1):
        """Polyline (already sampled points). closed=True + fill=color for filled shapes."""
        P = np.asarray(P, float)
        if len(P) < 2:
            return
        P = wobble(P, seed, wob, closed)
        pts = self._p(P)
        if closed:
            if fill is not None and len(pts) >= 3:
                self.d.polygon(pts, fill=fill)
            pts = pts + [pts[0]]
        if w <= 0 or color is None:
            return
        W = self._w(w)
        self.d.line(pts, fill=color, width=W, joint="curve")
        if not closed:
            r = W / 2
            for x, y in (pts[0], pts[-1]):
                self.d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def line(self, p1, p2, w=SW, color=INK, wob=1.0, seed=1):
        self.stroke(np.linspace(np.asarray(p1, float), np.asarray(p2, float), 12), w, color, wob=wob, seed=seed)

    def curve(self, points, w=SW, color=INK, closed=False, fill=None, wob=1.0, seed=1):
        """Smooth Catmull-Rom curve through the given points."""
        self.stroke(catmull(points, 10, closed), w, color, closed, fill, wob, seed)

    def polygon(self, points, w=SW, color=INK, fill=None, wob=0.0, seed=1):
        self.stroke(points, w, color, True, fill, wob, seed)

    def circle(self, c, r, w=SW, color=INK, fill=None, wob=0.6, seed=1):
        self.stroke(ellipse_pts(c, r, r, n=36), w, color, True, fill, wob, seed)

    def ellipse(self, c, rx, ry, ang=0.0, w=SW, color=INK, fill=None, wob=0.6, seed=1):
        self.stroke(ellipse_pts(c, rx, ry, ang, n=40), w, color, True, fill, wob, seed)

    def dot(self, c, r, color=INK):
        x, y = self.tp(c[0], c[1])
        r = r * self.k * self.zoom()
        self.d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    def rect(self, c, wd, ht, ang=0.0, radius=0.0, w=SW, color=INK, fill=None, wob=0.0, seed=1):
        """Rectangle centered at c, optional rotation (rad) and rounded corners."""
        c = np.asarray(c, float)
        hw, hh = wd / 2, ht / 2
        r = min(radius, hw, hh)
        if r <= 0:
            pts = [V(-hw, -hh), V(hw, -hh), V(hw, hh), V(-hw, hh)]
        else:
            pts = []
            for cx, cy, a0 in ((hw - r, -hh + r, -90), (hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180)):
                for a in np.linspace(math.radians(a0), math.radians(a0 + 90), 6):
                    pts.append(V(cx + r * math.cos(a), cy + r * math.sin(a)))
        pts = [c + rot(p, ang) for p in pts]
        self.stroke(pts, w, color, True, fill, wob, seed)

    def arrow(self, p1, p2, w=SW, color=INK, head=None, bend=0.0, wob=0.8, seed=1):
        """Arrow from p1 to p2. bend>0 curves it (fraction of length)."""
        p1, p2 = np.asarray(p1, float), np.asarray(p2, float)
        d = p2 - p1
        L = np.linalg.norm(d) + 1e-9
        m = (p1 + p2) / 2 + np.array([-d[1], d[0]]) / L * bend * L
        P = noodle(p1, m, p2, 24)
        self.stroke(P, w, color, wob=wob, seed=seed)
        tip_dir = P[-1] - P[-3]
        tip_dir /= np.linalg.norm(tip_dir) + 1e-9
        hl = head or max(18.0, 0.12 * L)
        for s in (-0.45, 0.45):
            self.line(p2, p2 - rot(tip_dir, s) * hl, w, color, wob=0)

    def star(self, c, r, n=5, ang=0.0, w=SW, color=INK, fill=YELLOW, seed=1):
        pts = []
        for i in range(2 * n):
            rr = r if i % 2 == 0 else r * 0.45
            a = ang - math.pi / 2 + i * math.pi / n
            pts.append(np.asarray(c, float) + V(math.cos(a) * rr, math.sin(a) * rr))
        self.stroke(pts, w, color, True, fill, 0, seed)

    def heart(self, c, r, w=SW, color=INK, fill=PINK, seed=1):
        c = np.asarray(c, float)
        tt = np.linspace(0, 2 * np.pi, 48, endpoint=False)
        x = 16 * np.sin(tt) ** 3
        y = -(13 * np.cos(tt) - 5 * np.cos(2 * tt) - 2 * np.cos(3 * tt) - np.cos(4 * tt))
        P = np.stack([x, y], 1) / 16 * r + c
        self.stroke(P, w, color, True, fill, 0, seed)

    def text(self, xy, s, size=60, color=INK, anchor="mm"):
        """anchor: Pillow anchors, e.g. 'mm' center, 'lm' left-middle, 'rm' right-middle."""
        f = get_font(size * self.k * self.zoom())
        self.d.text(self.tp(xy[0], xy[1]), s, font=f, fill=color, anchor=anchor)

    def text_width(self, s, size=60):
        f = get_font(size * self.k * self.zoom())
        return self.d.textlength(s, font=f) / (self.k * self.zoom())


# =============================================================== pose library
# Arms are defined by a HAND TARGET relative to the shoulder in polar form:
#   (angle, reach)  angle: 0 = straight down, +pi/2 = horizontal outward, pi = straight up,
#                   negative = across the body (inward). reach: 0..1 of full arm length.
# Left arm is mirrored, so the same numbers give a symmetric pose. Elbows are solved by IK.
# Other fields: crouch (-0.2 stretch .. 1 deep squat), tilt (head, rad, + = toward screen-right),
# lean (whole upper body, rad), hipx (hip sway, fraction of height), stance (feet width mult),
# liftl / liftr (raise a foot 0..1), sit (0..1, sit on a chair/seat; pair with the 'chair' prop),
# kneel (0..1, left knee on the ground), lh / rh hand shapes:
HAND_SHAPES = ["open", "fist", "point", "thumb", "peace", "relaxed"]

POSE_DEFAULTS = dict(la=0.18, lr=0.93, ra=0.18, rr=0.93, crouch=0.0, tilt=0.0, lean=0.0,
                     hipx=0.0, stance=1.0, liftl=0.0, liftr=0.0, sit=0.0, kneel=0.0,
                     lh="relaxed", rh="relaxed")


def _pose(r=(0.18, 0.93), l=(0.18, 0.93), **kw):
    d = dict(POSE_DEFAULTS)
    d.update(ra=r[0], rr=r[1], la=l[0], lr=l[1])
    d.update(kw)
    return d


POSES = {
    "rest":          _pose(),
    "hands_on_hips": _pose((0.19, 0.79), (0.19, 0.79), rh="fist", lh="fist"),
    "wave":          _pose((2.43, 0.81), (0.25, 0.92), rh="open"),
    "wave_both":     _pose((2.43, 0.81), (2.43, 0.81), rh="open", lh="open"),
    "point_up":      _pose((2.72, 0.99), (0.30, 0.90), rh="point"),
    "point_side":    _pose((1.65, 0.99), (0.20, 0.92), rh="point"),
    "point_down":    _pose((0.55, 0.99), (0.20, 0.92), rh="point"),
    "think":         _pose((-1.62, 0.33), (-0.69, 0.64), rh="fist", lh="open", tilt=0.12),
    "facepalm":      _pose((-2.45, 0.58), (0.20, 0.90), rh="open", tilt=0.18, crouch=0.08),
    "phone":         _pose((2.73, 0.72), (0.20, 0.92), rh="fist", tilt=0.10),
    "shrug":         _pose((1.25, 0.65), (1.25, 0.65), rh="open", lh="open", tilt=0.12),
    "cheer":         _pose((2.36, 0.99), (2.36, 0.99), rh="fist", lh="fist", crouch=-0.12),
    "victory":       _pose((2.93, 0.99), (0.30, 0.90), rh="fist", crouch=-0.08),
    "fist_pump":     _pose((2.20, 0.56), (0.19, 0.79), rh="fist", lh="fist", crouch=0.05),
    "clap":          _pose((-0.64, 0.61), (-0.64, 0.61), rh="open", lh="open"),
    "cross_arms":    _pose((-0.90, 0.79), (-0.98, 0.74), rh="fist", lh="fist"),
    "hands_on_head": _pose((3.14, 0.92), (3.14, 0.92), rh="open", lh="open"),
    "scared":        _pose((-2.82, 0.52), (-2.82, 0.52), rh="open", lh="open", crouch=0.3),
    "stop":          _pose((2.60, 0.48), (0.20, 0.92), rh="open"),
    "present":       _pose((1.28, 0.86), (0.20, 0.92), rh="open", tilt=-0.06),
    "welcome":       _pose((1.37, 0.84), (1.37, 0.84), rh="open", lh="open", crouch=-0.05),
    "thumbs_up":     _pose((1.99, 0.40), (0.20, 0.92), rh="thumb"),
    "peace":         _pose((2.45, 0.62), (0.20, 0.92), rh="peace", tilt=-0.1),
    "angry":         _pose((0.26, 0.80), (0.26, 0.80), rh="fist", lh="fist", crouch=0.12),
    "sad":           _pose((0.04, 0.94), (0.04, 0.94), tilt=0.22, crouch=0.12),
    "tired":         _pose((0.05, 0.97), (0.05, 0.97), tilt=0.25, crouch=0.18),
    "flex":          _pose((2.14, 0.68), (2.14, 0.68), rh="fist", lh="fist", crouch=0.05),
    "type":          _pose((-0.23, 0.88), (-0.23, 0.88), rh="open", lh="open", crouch=0.12),
    "carry":         _pose((-0.41, 0.72), (-0.41, 0.72), rh="open", lh="open", crouch=0.05),
    "hold_up":       _pose((2.36, 0.70), (2.36, 0.70), rh="fist", lh="fist"),
    "pray":          _pose((-1.15, 0.40), (-1.15, 0.40), rh="open", lh="open", tilt=0.08),
    "reach_up":      _pose((3.06, 0.99), (3.06, 0.99), rh="open", lh="open", crouch=-0.15),
    "surrender":     _pose((2.31, 0.67), (2.31, 0.67), rh="open", lh="open"),
    "explain":       _pose((1.25, 0.52), (0.20, 0.92), rh="open", tilt=-0.05),
    "dance_a":       _pose((2.39, 0.90), (0.52, 0.66), rh="open", lh="fist", tilt=0.15, hipx=0.04, crouch=0.08),
    "dance_b":       _pose((0.52, 0.66), (2.39, 0.90), rh="fist", lh="open", tilt=-0.15, hipx=-0.04, crouch=0.08),
    "crouch":        _pose((0.40, 0.80), (0.40, 0.80), crouch=0.7),
    "jump_prep":     _pose((0.70, 0.95), (0.70, 0.95), rh="fist", lh="fist", crouch=0.6),
    "kick":          _pose((0.90, 0.90), (0.90, 0.90), liftr=1.0, tilt=-0.05),
    "balance":       _pose((1.57, 0.99), (1.57, 0.99), rh="open", lh="open", liftl=0.8),
    # --- extra everyday scenarios ---
    "salute":        _pose((-2.90, 0.78), (0.15, 0.94), rh="open", tilt=-0.03, crouch=-0.04),
    "bow":           _pose((0.02, 0.95), (0.02, 0.95), lean=0.45, tilt=0.20, crouch=0.06),
    "head_scratch":  _pose((-3.00, 0.88), (0.20, 0.92), rh="relaxed", tilt=0.15, hipx=0.02),
    "shush":         _pose((-2.10, 0.45), (0.20, 0.92), rh="point", tilt=-0.05),
    "listen":        _pose((2.65, 0.66), (0.20, 0.92), rh="open", tilt=0.18, lean=0.06),
    "look_far":      _pose((-2.75, 0.68), (0.25, 0.90), rh="open", lean=0.06, crouch=-0.05),
    "drink":         _pose((-2.00, 0.48), (0.20, 0.92), rh="fist", tilt=-0.20),
    "stretch":       _pose((2.95, 0.99), (-2.95, 0.99), rh="open", lh="open", lean=0.18, tilt=0.10, hipx=-0.03, crouch=-0.10),
    "point_you":     _pose((1.35, 0.55), (0.20, 0.92), rh="point", lean=0.05, tilt=-0.05),
    "cry":           _pose((-2.60, 0.55), (-2.60, 0.55), rh="fist", lh="fist", tilt=0.12, crouch=0.15),
    # --- everyday actions ---
    "write":         _pose((-0.55, 0.55), (-0.75, 0.62), rh="fist", lh="open", tilt=0.15),
    "read":          _pose((-0.95, 0.62), (-0.95, 0.62), rh="open", lh="open", tilt=0.15),
    "push":          _pose((1.45, 0.95), (-1.35, 0.99), rh="open", lh="open", lean=0.25, crouch=0.2, stance=1.5),
    "handshake":     _pose((1.00, 0.75), (0.20, 0.92), rh="fist", lean=0.05),
    # --- emotions / reactions ---
    "shocked":       _pose((0.85, 0.99), (0.85, 0.99), rh="open", lh="open", lean=-0.12, crouch=-0.10),
    "laugh":         _pose((-0.50, 0.55), (0.30, 0.90), rh="open", tilt=-0.20, lean=-0.08),
    "nervous":       _pose((-0.85, 0.55), (-0.85, 0.55), rh="open", lh="open", tilt=0.08, crouch=0.10),
    "embarrassed":   _pose((2.85, 0.80), (0.20, 0.92), rh="open", tilt=0.12, hipx=0.02),
    # --- communication / presenting ---
    "call_out":      _pose((-2.20, 0.50), (-2.20, 0.50), rh="open", lh="open", tilt=-0.10, lean=-0.05),
    "beckon":        _pose((1.40, 0.60), (0.20, 0.92), rh="open", lean=0.05),
    "measure":       _pose((1.05, 0.85), (1.05, 0.85), rh="open", lh="open", tilt=-0.05),
    "point_screen":  _pose((0.20, 0.92), (2.10, 0.99), lh="point", tilt=-0.10, lean=-0.05),
    # --- action ---
    "punch":         _pose((1.57, 0.99), (-1.20, 0.45), rh="fist", lh="fist", lean=0.12, crouch=0.15, stance=1.5),
    "fight_stance":  _pose((-1.90, 0.50), (-1.90, 0.50), rh="fist", lh="fist", crouch=0.25, stance=1.5, tilt=0.05),
    "fall_back":     _pose((2.60, 0.95), (2.20, 0.95), rh="open", lh="open", lean=-0.35, tilt=-0.15, liftr=0.7),
    # --- seated / kneeling (use the sit / kneel fields) ---
    "sit":           _pose((0.30, 0.80), (0.30, 0.80), sit=1.0),
    "kneel":         _pose((0.30, 0.90), (0.30, 0.90), kneel=1.0, tilt=0.05),
    # --- productivity / planning / education / youtube ---
    "present_both":  _pose((1.35, 0.88), (1.35, 0.88), rh="open", lh="open", tilt=-0.04),
    "plan_board":    _pose((2.15, 0.92), (-1.55, 0.35), rh="point", lh="fist", tilt=-0.06),
    "film":          _pose((1.55, 0.78), (0.30, 0.90), rh="fist", tilt=-0.05),
    "podcast":       _pose((1.15, 0.58), (-0.55, 0.55), rh="open", lh="open", lean=0.06),
    "meditate":      _pose((-0.85, 0.55), (-0.85, 0.55), rh="peace", lh="peace", sit=0.8, crouch=0.2),
    "juggle":        _pose((1.20, 0.65), (1.20, 0.65), rh="open", lh="open", crouch=0.06),
    "graduate":      _pose((2.85, 0.98), (-0.65, 0.58), rh="open", lh="fist", crouch=-0.08),
    "celebrate_jump": _pose((2.55, 0.99), (2.55, 0.99), rh="open", lh="open", liftr=0.85, crouch=-0.12),
}

# =============================================================== face library
FACE_DEFAULTS = dict(smile=0.3, mopen=0.0, brow=0.0, btilt=0.0, lookx=0.0, looky=0.0,
                     eyes=1.0, happy=0.0, closed=0.0)


def _face(**kw):
    d = dict(FACE_DEFAULTS)
    d.update(kw)
    return d


FACES = {
    "neutral":    _face(),
    "happy":      _face(smile=0.8, mopen=0.15, brow=0.2),
    "very_happy": _face(smile=1.0, mopen=0.5, brow=0.4, happy=1),
    "laugh":      _face(smile=1.0, mopen=0.85, brow=0.5, happy=1),
    "excited":    _face(smile=1.0, mopen=0.6, brow=0.8, eyes=1.3),
    "proud":      _face(smile=0.8, brow=0.4, eyes=0.8, looky=-0.3),
    "smug":       _face(smile=0.55, brow=-0.1, btilt=-0.3, eyes=0.7, lookx=0.4),
    "sad":        _face(smile=-0.7, btilt=0.9, brow=0.1, looky=0.5),
    "worried":    _face(smile=-0.35, btilt=0.8, brow=0.3),
    "scared":     _face(smile=-0.4, mopen=0.45, btilt=0.9, brow=0.6, eyes=1.4),
    "angry":      _face(smile=-0.5, mopen=0.1, btilt=-1.0, brow=-0.3),
    "furious":    _face(smile=-0.6, mopen=0.5, btilt=-1.2, brow=-0.4),
    "surprised":  _face(smile=0.0, mopen=0.55, brow=1.0, eyes=1.35),
    "shocked":    _face(smile=-0.1, mopen=1.0, brow=1.2, eyes=1.6),
    "thinking":   _face(smile=-0.1, brow=0.4, lookx=0.7, looky=-1.0),
    "focused":    _face(smile=0.0, btilt=-0.5, brow=-0.2, looky=0.8, eyes=0.85),
    "confused":   _face(smile=-0.2, brow=0.5, btilt=0.4, lookx=-0.5),
    "disgusted":  _face(smile=-0.6, btilt=-0.6, lookx=0.6),
    "sleepy":     _face(smile=0.0, mopen=0.1, closed=1),
    "love":       _face(smile=1.0, mopen=0.2, brow=0.5, happy=1),
    "confident":  _face(smile=0.75, brow=0.25, btilt=-0.15, eyes=0.9),
    "determined": _face(smile=0.2, btilt=-0.6, brow=-0.2, eyes=0.95),
    "calm":       _face(smile=0.45, brow=0.1, eyes=0.85),
    "curious":    _face(smile=0.2, brow=0.6, btilt=0.35, eyes=1.15, lookx=0.3),
    "serious":    _face(smile=-0.1, btilt=-0.4, brow=-0.15, eyes=0.9),
    "tired":      _face(smile=-0.25, mopen=0.15, btilt=0.5, eyes=0.6),
    "bored":      _face(smile=-0.15, brow=-0.1, eyes=0.65, lookx=0.4, looky=-0.2),
    "embarrassed": _face(smile=0.4, btilt=0.6, brow=0.3, lookx=-0.4, looky=0.3),
    "nervous":    _face(smile=-0.2, mopen=0.2, btilt=0.75, brow=0.4, eyes=1.2),
}

LAYER_KINDS = ["wave", "wave_l", "wave_both", "talk", "nod", "shake_head", "tremble", "bounce",
               "clap", "type", "laugh", "look_around", "float", "breathe_hard", "write", "juggle"]


# =============================================================== character
def _fold(keys, base, t):
    """Evaluate a keyframe track. keys: (t_start, t_end, target_dict, ease), sorted by t_start.
    Each key blends from the current value to its target between t_start and t_end, then holds."""
    cur = dict(base)
    for ts, te, tgt, ease in keys:
        if t < ts:
            break
        u = 1.0 if te <= ts else (t - ts) / (te - ts)
        w = 1.0 if u >= 1 else EASES.get(ease, ease_io)(u)
        for k, v in tgt.items():
            if isinstance(v, str):
                if u >= 0.5:
                    cur[k] = v
            else:
                cur[k] = lerp(cur.get(k, v), v, w)
    return cur


_GROUPS = {"right": ("ra", "rr", "rh"), "left": ("la", "lr", "lh"),
           "body": ("crouch", "tilt", "lean", "hipx", "stance", "liftl", "liftr", "sit", "kneel")}
_GROUPS["arms"] = _GROUPS["right"] + _GROUPS["left"]
_GROUPS["legs"] = ("stance", "liftl", "liftr", "crouch", "hipx", "sit", "kneel")


class Character:
    """A stickman. All timeline methods take a start time in seconds and RETURN THEIR END TIME,
    so actions can be chained: t = bob.walk_to(1.0, 0.7); bob.pose(t, "wave")."""

    def __init__(self, scene, name, x=0.5, ground=0.86, scale=1.0, color=INK, facing=0, z=0, visible=True):
        self.scene, self.name, self.color, self.z = scene, name, color, z
        self.h = HC * scale
        self.x0, self.g0 = x * BW, ground * BH
        self.pose_keys, self.face_keys, self.move_keys = [], [], []
        self.turn_keys, self.scale_keys = [], []
        self.layers, self.walks, self.jumps, self.holds = [], [], [], []
        self.base_pose, self.base_face = dict(POSES["rest"]), dict(FACES["neutral"])
        self.facing0 = facing
        self.scale0 = 1.0 if visible else 0.0
        self.seed = sd(name)
        r = random.Random(self.seed)
        self.blinks, tb = [], r.uniform(0.6, 2.0)
        while tb < 900:
            self.blinks.append(tb)
            tb += r.uniform(2.0, 4.5)
        self._cache_t, self._cache = None, None

    # ------------------------------------------------------------ timeline API
    @staticmethod
    def _add(track, key):
        track.append(key)
        track.sort(key=lambda k: k[0])

    def pose(self, t, name, dur=0.4, ease="io", face=None, only=None, **override):
        """Blend into a library pose starting at t over dur seconds, then hold it.
        only: 'arms' | 'right' | 'left' | 'body' | 'legs' to apply part of the pose.
        override: r=(angle, reach), l=(angle, reach), rh='fist', crouch=0.3, tilt=..., etc."""
        if name not in POSES:
            raise ValueError(f"Unknown pose '{name}'. Available: {sorted(POSES)}")
        tgt = dict(POSES[name])
        for k, v in override.items():
            if k in ("r", "l"):
                tgt[k + "a"], tgt[k + "r"] = float(v[0]), float(v[1])
            elif k in tgt:
                tgt[k] = v
            else:
                raise ValueError(f"Unknown pose field '{k}'")
        if only:
            keep = set(_GROUPS[only])
            tgt = {k: v for k, v in tgt.items() if k in keep}
        self._add(self.pose_keys, (t, t + dur, tgt, ease))
        if face:
            self.expression(t, face, dur=min(dur, 0.3))
        self._cache_t = None
        return t + dur

    def set(self, t, dur=0.3, ease="io", **fields):
        """Low-level partial key, e.g. bob.set(2, r=(1.2, 0.8), crouch=0.2, rh='fist')."""
        tgt = {}
        for k, v in fields.items():
            if k in ("r", "l"):
                tgt[k + "a"], tgt[k + "r"] = float(v[0]), float(v[1])
            else:
                tgt[k] = v
        self._add(self.pose_keys, (t, t + dur, tgt, ease))
        return t + dur

    def expression(self, t, name, dur=0.25, ease="io", **override):
        """Blend the face to a preset from FACES (override fields like smile=0.5 allowed)."""
        if name not in FACES:
            raise ValueError(f"Unknown face '{name}'. Available: {sorted(FACES)}")
        tgt = dict(FACES[name])
        tgt.update(override)
        self._add(self.face_keys, (t, t + dur, tgt, ease))
        return t + dur

    def look(self, t, x=0.0, y=0.0, dur=0.2):
        """Move eyes: x -1 (left) .. 1 (right), y -1 (up) .. 1 (down)."""
        self._add(self.face_keys, (t, t + dur, {"lookx": x, "looky": y}, "io"))
        return t + dur

    def layer(self, kind, t0, t1, amount=1.0):
        """Additive looping motion between t0 and t1 (fades in/out 0.2 s). Kinds: LAYER_KINDS."""
        if kind not in LAYER_KINDS:
            raise ValueError(f"Unknown layer '{kind}'. Available: {LAYER_KINDS}")
        self.layers.append((kind, t0, t1, amount))
        return t1

    def _root(self, t):
        return _fold(self.move_keys, {"x": self.x0, "y": self.g0}, t)

    def walk_to(self, t, x, dur=None, run=False, ease="io", face_front=True, sfx=True):
        """Walk (or run) to normalized x (0..1) with a stepping leg cycle and arm swing.
        sfx: footstep sounds (True/False)."""
        x0 = self._root(t)["x"]
        x1 = x * BW
        if dur is None:
            dur = max(0.5, abs(x1 - x0) / ((0.42 if run else 0.2) * BW))
        self._add(self.move_keys, (t, t + dur, {"x": x1}, ease))
        self.walks.append((t, t + dur, "run" if run else "walk"))
        if sfx:
            N = max(2, int(round(dur / (0.17 if run else 0.27))))
            for j in range(N):
                ts = t + (j + 1) * dur / N
                xs = lerp(x0, x1, (j + 1) / N) / BW
                self.scene._sfx(ts, "step", 0.35 if run else 0.25, self.scene._pan(xs), 0.9 + 0.2 * (j % 2))
        if abs(x1 - x0) > 1:
            self.turn(t, 1 if x1 > x0 else -1, 0.2)
            if face_front:
                self.turn(t + dur, 0, 0.3)
        return t + dur

    def run_to(self, t, x, dur=None, ease="io", face_front=True, sfx=True):
        return self.walk_to(t, x, dur, True, ease, face_front, sfx)

    def move_to(self, t, x=None, y=None, dur=0.6, ease="io"):
        """Slide/float to normalized x and/or ground y (no leg cycle)."""
        tgt = {}
        if x is not None:
            tgt["x"] = x * BW
        if y is not None:
            tgt["y"] = y * BH
        self._add(self.move_keys, (t, t + dur, tgt, ease))
        return t + dur

    def _pan(self, t):
        return self.scene._pan(self._root(t)["x"] / BW)

    def jump(self, t, height=0.35, dur=0.55, sfx=True):
        """Jump with anticipation squash, airtime dur, landing squash. height in character heights.
        sfx: 'boing' on take-off + 'thud' on landing (True/False or a SOUNDS name for take-off)."""
        self.jumps.append((t, height, dur))
        if sfx:
            self.scene._sfx(t + 0.12, sfx if isinstance(sfx, str) else "boing", 0.7, self._pan(t))
            self.scene._sfx(t + 0.14 + dur, "thud", 0.6, self._pan(t))
        return t + 0.14 + dur + 0.2

    def turn(self, t, facing, dur=0.25):
        """facing: -1 look left, 0 front, 1 look right (shifts face features)."""
        self._add(self.turn_keys, (t, t + dur, {"f": float(facing)}, "io"))
        return t + dur

    def show(self, t, dur=0.4, sfx="pop"):
        self._add(self.scale_keys, (t, t + dur, {"s": 1.0}, "elastic"))
        self.scene._sfx(t, sfx or None, 0.8, self._pan(t))
        return t + dur

    def hide(self, t, dur=0.25, sfx="swish"):
        self._add(self.scale_keys, (t, t + dur, {"s": 0.0}, "in"))
        self.scene._sfx(t, sfx or None, 0.7, self._pan(t))
        return t + dur

    def hold(self, prop, t0, t1, hand="r", scale=1.0, **kw):
        """Attach a prop from PROPS to a hand ('r' or 'l') between t0 and t1."""
        if prop not in PROPS:
            raise ValueError(f"Unknown prop '{prop}'. Available: {sorted(PROPS)}")
        self.holds.append((prop, t0, t1, hand, scale, kw))
        return t1

    # ------------------------------------------------------------ evaluation
    def _walk_state(self, t):
        for ts, te, kind in self.walks:
            if ts <= t <= te:
                T0 = 0.17 if kind == "run" else 0.27
                N = max(2, int(round((te - ts) / T0)))
                T = (te - ts) / N
                n = min(N - 1, int((t - ts) / T))

                def plant(j):
                    return self._root(ts)["x"] if j < 0 else self._root(min(te, ts + (j + 2) * T))["x"]

                res = {}
                for k, side in ((0, -1), (1, 1)):
                    m = n if n % 2 == k else n - 1
                    if m == n:
                        u = clamp((t - (ts + n * T)) / T)
                        res[side] = (lerp(plant(m - 2), plant(m), ease_io(u)), math.sin(math.pi * u))
                    else:
                        res[side] = (plant(m), 0.0)
                return res, abs(math.sin(math.pi * (t - ts) / T)), (t - ts) / T, kind
        return None

    def rig(self, t):
        if self._cache_t == t:
            return self._cache
        p = _fold(self.pose_keys, self.base_pose, t)
        lag = _fold(self.pose_keys, self.base_pose, t - 0.08)       # follow-through
        for k in ("la", "ra", "lr", "rr", "tilt"):
            p[k] = 0.6 * p[k] + 0.4 * lag[k]
        f = _fold(self.face_keys, self.base_face, t)
        root = self._root(t)
        rx, gy = root["x"], root["y"]
        facing = _fold(self.turn_keys, {"f": self.facing0}, t)["f"]
        sc = _fold(self.scale_keys, {"s": self.scale0}, t)["s"]
        hh = self.h * max(sc, 0.02)
        TT = 2 * math.pi * t

        # jumps
        dy, air, cadd = 0.0, 0.0, 0.0
        for tj, hgt, dj in self.jumps:
            a1, a2 = tj + 0.14, tj + 0.14 + dj
            if tj <= t < a1:
                cadd += 0.5 * math.sin(math.pi / 2 * (t - tj) / 0.14)
            elif a1 <= t < a2:
                ub = (t - a1) / dj
                air = math.sin(math.pi * ub)
                dy -= air * hgt * hh
                cadd += 0.5 * max(0.0, 1 - ub * 6)
            elif a2 <= t < a2 + 0.2:
                cadd += 0.4 * math.sin(math.pi * (t - a2) / 0.2)

        # additive layers
        lay = {}
        for kind, t0, t1, amt in self.layers:
            if t0 <= t <= t1:
                env = clamp((t - t0) / 0.2) * clamp((t1 - t) / 0.2)
                lay[kind] = max(lay.get(kind, 0.0), amt * env)

        def A(k):
            return lay.get(k, 0.0)

        p["ra"] += (A("wave") + A("wave_both")) * 0.28 * math.sin(TT * 2.4)
        p["la"] += (A("wave_l") + A("wave_both")) * 0.28 * math.sin(TT * 2.4 + 0.8)
        cl = A("clap") * 0.35 * abs(math.sin(TT * 2.5))
        p["ra"] += cl
        p["la"] += cl
        p["rr"] -= A("type") * 0.07 * max(0.0, math.sin(TT * 6))
        p["lr"] -= A("type") * 0.07 * max(0.0, math.sin(TT * 6 + math.pi))
        p["ra"] += A("write") * 0.14 * math.sin(TT * 6.5) + A("juggle") * 0.34 * math.sin(TT * 3.2)
        p["rr"] += A("write") * 0.05 * math.cos(TT * 8.5) + A("juggle") * 0.08 * math.cos(TT * 3.2)
        p["la"] += A("juggle") * 0.34 * math.sin(TT * 3.2 + math.pi)
        p["lr"] += A("juggle") * 0.08 * math.cos(TT * 3.2 + math.pi)
        p["crouch"] += (A("bounce") * 0.15 * (0.5 + 0.5 * math.sin(TT * 2))
                        + A("laugh") * 0.08 * abs(math.sin(TT * 4)) + cadd)
        p["tilt"] += A("bounce") * 0.08 * math.sin(TT) - A("laugh") * 0.05
        head_dx = A("shake_head") * 0.02 * math.sin(TT * 3)
        head_dy = A("nod") * 0.025 * (0.5 + 0.5 * math.sin(TT * 2.5)) + A("type") * 0.004 * math.sin(TT * 6)
        f["lookx"] += A("shake_head") * 0.6 * math.sin(TT * 3) + A("look_around") * math.sin(TT * 0.6)
        f["looky"] += A("nod") * 0.5 * math.sin(TT * 2.5)
        if A("talk"):
            f["mopen"] = max(f["mopen"], A("talk") * (0.12 + 0.4 * abs(math.sin(TT * 4.3) * math.sin(TT * 1.7 + 1))))
        if A("laugh"):
            f["mopen"] = max(f["mopen"], A("laugh") * (0.4 + 0.4 * abs(math.sin(TT * 4))))
            f["happy"] = max(f["happy"], A("laugh"))
        trem = A("tremble")
        hipjit = trem * 0.006 * math.sin(TT * 14)
        p["ra"] += trem * 0.04 * math.sin(TT * 13)
        p["la"] += trem * 0.04 * math.sin(TT * 15)
        dy -= A("float") * 0.04 * hh * (0.5 + 0.5 * math.sin(TT * 0.8))
        ph = (self.seed % 100) / 10.0
        for i, k in enumerate(("la", "ra", "tilt")):                 # alive drift
            p[k] += 0.03 * math.sin(TT * 0.45 * (1 + 0.2 * i) + i * 2 + ph)
        breath = math.sin(TT / 2.6 * (1 + A("breathe_hard") * 2.5)) * (1 + 3 * A("breathe_hard"))
        blink = any(abs(t - b) < 0.06 for b in self.blinks if b < t + 1)

        # walk cycle
        feet = {-1: (None, 0.0), 1: (None, 0.0)}
        walk_bob, running = 0.0, False
        wk = self._walk_state(t)
        if wk:
            feet, bobv, phase, kind = wk
            running = kind == "run"
            sw = (0.7 if running else 0.35) * math.sin(math.pi * phase)
            p["ra"] += sw
            p["la"] -= sw
            walk_bob = bobv * (0.03 if running else 0.015)
            if running:
                p["crouch"] += 0.1

        # geometry
        crouch = p["crouch"]
        sq = 1 - 0.10 * crouch + 0.05 * air
        leg = 0.25 * hh
        th = 0.42 * hh * sq * (1 + 0.012 * breath)
        tw = 0.28 * hh * (1 + 0.6 * (1 - sq))
        lean = p["lean"]
        up = V(math.sin(lean), -math.cos(lean))
        side = V(-up[1], up[0])
        sit, kneel = clamp(p.get("sit", 0.0)), clamp(p.get("kneel", 0.0))
        B = V(rx + (p["hipx"] + hipjit) * hh,
              gy - 0.93 * leg + 0.02 * hh + crouch * 0.09 * hh + (0.30 * sit + 0.40 * kneel) * leg
              + dy - walk_bob * hh)

        def S(u):
            return B + up * th * u

        def wd(u):
            return tw / 2 * (1.0 - 0.22 * u ** 2)

        legs = {}
        for sx in (-1, 1):
            hip = B + up * 0.02 * hh + side * sx * 0.065 * hh
            lift = p["liftl"] if sx < 0 else p["liftr"]
            fxw, wl = feet[sx]
            spread = p["stance"] + 0.9 * sit + (0.6 * kneel if sx > 0 else 0.0)
            fx = (rx if fxw is None else fxw) + sx * 0.085 * hh * spread + sx * 0.03 * hh * lift
            fy = gy - lift * 0.4 * leg - wl * (0.35 if running else 0.22) * leg
            fy = min(fy, hip[1] + lerp(0.98, 0.75, air) * leg)
            fold = sit if sx < 0 else max(sit, kneel)
            knee, foot = ik2(hip, V(fx, fy), 0.125 * hh, 0.125 * hh, V(sx, lerp(-0.3, -1.6, fold)))
            if sx < 0 and kneel > 0.001:                      # left knee down, shin flat behind
                kn2 = V(hip[0] - 0.02 * hh, gy - 0.012 * hh)
                ft2 = kn2 + V(-0.11 * hh, 0.008 * hh)
                knee, foot = lerp(knee, kn2, kneel), lerp(foot, ft2, kneel)
            legs[sx] = (hip, knee, foot)

        ctrl = [S(u) + side * wd(u) for u in (0.08, 0.45, 0.8, 0.97)]
        ctrl.append(S(1.0) + up * 0.02 * hh)
        ctrl += [S(u) - side * wd(u) for u in (0.97, 0.8, 0.45, 0.08)]
        ctrl.append(S(0.0) - up * 0.035 * hh)

        ang = lean + p["tilt"]
        C = S(1.0) + up * 0.13 * hh + V(head_dx * hh, head_dy * hh)

        arms = {}
        for sx, a, rch, hs in ((-1, p["la"], p["lr"], p["lh"]), (1, p["ra"], p["rr"], p["rh"])):
            Sh = S(0.86) + side * sx * wd(0.86) * 0.9 + up * 0.004 * hh * breath
            dirv = rot(V(sx * math.sin(a), math.cos(a)), lean)
            target = Sh + dirv * clamp(rch, 0.05, 1.0) * 0.29 * hh * 0.999
            E, Hn = ik2(Sh, target, 0.15 * hh, 0.14 * hh, V(sx * 0.5, 1.0))
            d2 = (Hn - E) / (np.linalg.norm(Hn - E) + 1e-9)
            arms[sx] = (Sh, E, Hn, d2, hs)

        R = dict(t=t, visible=sc > 0.01, hh=hh, p=p, f=f, facing=facing, rx=rx, gy=gy, B=B, up=up,
                 side=side, chest=S(0.6), torso=ctrl, C=C, ang=ang, legs=legs, arms=arms, sq=sq, blink=blink)
        self._cache_t, self._cache = t, R
        return R

    def anchor(self, t, name="head", offset=(0.0, 0.0)):
        """World point on the character. names: head, above_head, top, face, mouth, chest, hip, feet,
        hand_r, hand_l, left, right. offset is in character heights (x right, y down)."""
        R = self.rig(t)
        hh, C = R["hh"], R["C"]
        pts = {"head": C, "above_head": C + V(0, -0.42 * hh), "top": C + V(0, -0.19 * hh),
               "face": C + V(0, 0.02 * hh), "mouth": C + V(0, 0.07 * hh), "chest": R["chest"],
               "hip": R["B"], "feet": V(R["rx"], R["gy"]), "hand_r": R["arms"][1][2],
               "hand_l": R["arms"][-1][2], "left": C + V(-0.45 * hh, 0), "right": C + V(0.45 * hh, 0)}
        if name not in pts:
            raise ValueError(f"Unknown anchor '{name}'. Available: {sorted(pts)}")
        return pts[name] + V(offset[0], offset[1]) * hh

    # ------------------------------------------------------------ drawing
    def _hand(self, cv, Hn, d, shape, hh, col, seed):
        w = 0.0135 * hh
        if shape in ("open", "relaxed"):
            spread = (-0.7, -0.25, 0.2, 0.65) if shape == "open" else (-0.35, 0.0, 0.35)
            ln = 0.04 * hh if shape == "open" else 0.028 * hh
            for i, s in enumerate(spread):
                k = 0.8 if i in (0, len(spread) - 1) else 1.0
                cv.line(Hn, Hn + rot(d, s) * ln * k, w, col, wob=0)
        elif shape == "fist":
            cv.dot(Hn, 0.026 * hh, col)
        elif shape == "point":
            cv.dot(Hn, 0.022 * hh, col)
            cv.line(Hn, Hn + d * 0.055 * hh, w, col, wob=0)
        elif shape == "thumb":
            cv.dot(Hn, 0.026 * hh, col)
            cv.line(Hn, Hn + V(0, -0.05 * hh), w * 1.1, col, wob=0)
        elif shape == "peace":
            cv.dot(Hn, 0.02 * hh, col)
            for s in (-0.25, 0.25):
                cv.line(Hn, Hn + rot(d, s) * 0.05 * hh, w, col, wob=0)

    def _face(self, cv, R, col):
        hh, C, ang, f = R["hh"], R["C"], R["ang"], R["f"]
        shift = R["facing"] * 0.06

        def at(x, y):
            return C + rot(V((x + shift) * hh, y * hh), ang)

        w = 0.016 * hh
        lx, ly = f["lookx"] * 0.018, f["looky"] * 0.015
        el = 0.05 * f["eyes"]
        for sx in (-1, 1):
            ex, ey = sx * 0.075 + lx, -0.01 + ly
            if f["happy"] > 0.5:
                cv.curve([at(ex - 0.025, ey + 0.01), at(ex, ey - 0.015), at(ex + 0.025, ey + 0.01)], w * 0.9, col, wob=0)
            elif R["blink"] or f["closed"] > 0.5:
                cv.line(at(ex - 0.022, ey + 0.005), at(ex + 0.022, ey + 0.005), w * 0.9, col, wob=0)
            else:
                cv.line(at(ex, ey - el / 2), at(ex, ey + el / 2), w, col, wob=0)
            by = -0.09 - 0.03 * f["brow"]
            cv.line(at(sx * 0.045, by - 0.025 * f["btilt"]), at(sx * 0.11, by + 0.01 * f["btilt"]), w * 0.75, col, wob=0)
        sm, mo = f["smile"], f["mopen"]
        hw = 0.065 * (1 - 0.35 * mo * (1 - max(0.0, sm)))
        my = 0.07
        if mo < 0.08:
            cv.curve([at(-hw, my - 0.01 * sm), at(0, my + 0.035 * sm), at(hw, my - 0.01 * sm)], w * 0.9, col, wob=0.4)
        else:
            top = my - 0.01 * sm - 0.012 * mo * (1 - max(0.0, sm))
            bot = my + 0.035 * max(sm, 0.0) + 0.06 * mo
            cv.curve([at(-hw, my - 0.01 * sm), at(0, top), at(hw, my - 0.01 * sm), at(0, bot)],
                     w * 0.9, col, closed=True, fill=col, wob=0.3)

    def draw(self, cv, t):
        R = self.rig(t)
        if not R["visible"]:
            return
        hh, col = R["hh"], self.color
        w, wob = 0.018 * hh, 1.3 * hh / HC
        for sx in (-1, 1):
            hip, knee, foot = R["legs"][sx]
            cv.stroke(noodle(hip, knee, foot), w, col, wob=wob, seed=self.seed + 10 + sx)
            cv.ellipse(foot + V(sx * 0.03 * hh, 0.005 * hh), 0.045 * hh, 0.022 * hh, 0, w * 0.9, col, PAPER,
                       wob=0.5, seed=self.seed + 20 + sx)
        cv.stroke(catmull(R["torso"], 8, closed=True), w, col, True, PAPER, wob, self.seed + 1)
        rx_head = 0.20 * hh * (1 - 0.05 * abs(R["facing"]))
        cv.ellipse(R["C"], rx_head, 0.17 * hh * (0.97 + 0.03 * R["sq"]), R["ang"], w, col, PAPER, wob, self.seed + 2)
        self._face(cv, R, col)
        for sx in (-1, 1):
            Sh, E, Hn, d2, hs = R["arms"][sx]
            cv.stroke(noodle(Sh, E, Hn), w, col, wob=wob, seed=self.seed + 30 + sx)
            self._hand(cv, Hn, d2, hs, hh, col, self.seed + 40 + sx)
        for prop, t0, t1, hand, scale, kw in self.holds:
            if t0 <= t <= t1:
                Hn = R["arms"][1 if hand == "r" else -1][2]
                PROPS[prop](cv, Hn, scale * hh / HC, t - t0, **kw)


# =============================================================== props
# Every prop: fn(cv, c, s, lt, **kw). c = position (np.array), s = scale (1.0 matches a default-size
# character), lt = seconds since the prop appeared. Sizes below are design px at s=1.
def _w(s, k=1.0):
    return max(1.0, SW * s * k)


def prop_lightbulb(cv, c, s, lt, on=True, color=YELLOW):
    r = 34 * s
    if on:
        for j in range(7):
            a = -math.pi + j * math.pi / 6
            dv = V(math.cos(a), math.sin(a))
            cv.line(c + dv * r * 1.35, c + dv * r * (1.75 + 0.15 * math.sin(30 * lt + j)), _w(s, 0.8), wob=0)
    cv.ellipse(c, r, r * 1.05, 0, _w(s), INK, color if on else PAPER, seed=3)
    for j in range(3):
        y = c[1] + r * 1.05 + j * r * 0.22
        cv.line(V(c[0] - r * 0.42, y), V(c[0] + r * 0.42, y), _w(s, 0.8), wob=0)


def prop_laptop(cv, c, s, lt, label="$", color=LIGHT_GRAY):
    """c = bottom-center of the lid (put it on a surface)."""
    w, h = 165 * s, 72 * s
    cv.polygon([c + V(-w / 2, 0), c + V(-w / 2 + 4 * s, -h), c + V(w / 2 - 4 * s, -h), c + V(w / 2, 0)],
               _w(s), INK, color)
    cv.rect(c + V(0, 5 * s), w * 1.15, 10 * s, 0, 4 * s, _w(s, 0.8), INK, PAPER)
    if label:
        cv.text(c + V(0, -h / 2), label, 50 * s, GREEN)


def prop_desk(cv, c, s, lt, width=380, height=40):
    """c = floor point under the desk center."""
    top = c[1] - height * s
    for sx in (-1, 1):
        cv.line(V(c[0] + sx * width * 0.42 * s, top), V(c[0] + sx * width * 0.42 * s, c[1] + 8 * s), _w(s))
    cv.rect(V(c[0], top), width * s, 11 * s, 0, 0, _w(s), INK, PAPER)


def prop_workstation(cv, c, s, lt, label="$"):
    """Desk + laptop sized for the 'type' pose. Place at the character's feet: at=name, anchor='feet'."""
    prop_desk(cv, c, s, lt, width=380, height=0.0925 * HC)
    prop_laptop(cv, V(c[0], c[1] - 0.0925 * HC * s - 6 * s), s, lt, label)


def prop_money_bill(cv, c, s, lt, ang=0.0):
    cv.rect(c, 100 * s, 50 * s, ang, 3 * s, _w(s, 0.7), INK, LIGHT_GREEN)
    cv.circle(c, 14 * s, _w(s, 0.5), INK, None, wob=0)
    cv.text(c, "$", 26 * s, (30, 110, 40))


def prop_coin(cv, c, s, lt, flip=0.0):
    ang = (lt * 6.0 if flip else 0.0) if isinstance(flip, bool) else float(flip)
    sx = max(0.18, abs(math.cos(ang)))
    cv.ellipse(c, 18 * s * sx, 18 * s, 0, _w(s, 0.7), INK, GOLD, wob=0)
    if sx > 0.5:
        cv.text(c, "$", 22 * s, (150, 100, 0))


def prop_coin_stack(cv, c, s, lt, n=5):
    for i in range(int(n)):
        cv.ellipse(c + V(0, -i * 9 * s), 26 * s, 9 * s, 0, _w(s, 0.6), INK, GOLD, wob=0)


def prop_money_bag(cv, c, s, lt):
    c = c + V(0, 45 * s)
    cv.curve([c + V(-12 * s, -40 * s), c + V(-45 * s, 5 * s), c + V(-35 * s, 38 * s), c + V(35 * s, 38 * s),
              c + V(45 * s, 5 * s), c + V(12 * s, -40 * s)], _w(s), INK, closed=True, fill=(225, 200, 140), wob=0)
    cv.line(c + V(-16 * s, -40 * s), c + V(16 * s, -40 * s), _w(s), wob=0)
    cv.text(c + V(0, 8 * s), "$", 44 * s, GREEN)


def prop_phone(cv, c, s, lt):
    cv.rect(c + V(0, -12 * s), 26 * s, 46 * s, 0, 5 * s, _w(s, 0.7), INK, (60, 60, 70))
    cv.rect(c + V(0, -14 * s), 18 * s, 32 * s, 0, 2 * s, 0, None, LIGHT_BLUE)


def prop_coffee(cv, c, s, lt):
    cv.polygon([c + V(-16 * s, -22 * s), c + V(16 * s, -22 * s), c + V(12 * s, 14 * s), c + V(-12 * s, 14 * s)],
               _w(s, 0.8), INK, PAPER)
    cv.curve([c + V(15 * s, -14 * s), c + V(27 * s, -6 * s), c + V(14 * s, 6 * s)], _w(s, 0.7), wob=0)
    for i in range(2):
        x = c[0] + (i * 12 - 6) * s
        y0 = c[1] - 30 * s
        cv.curve([V(x, y0), V(x + 6 * s * math.sin(lt * 4 + i), y0 - 14 * s), V(x, y0 - 28 * s)], _w(s, 0.5), GRAY, wob=0)


def prop_sign(cv, c, s, lt, text="SALE!", color=PAPER):
    cv.line(c + V(0, 20 * s), c + V(0, -60 * s), _w(s))
    b = c + V(0, -110 * s)
    cv.rect(b, 230 * s, 105 * s, 0, 8 * s, _w(s), INK, color)
    cv.text(b, text, 46 * s)


def prop_box(cv, c, s, lt, label=""):
    cv.rect(c, 120 * s, 100 * s, 0, 4 * s, _w(s), INK, (222, 190, 140))
    cv.line(c + V(-60 * s, -30 * s), c + V(60 * s, -30 * s), _w(s, 0.6), wob=0)
    if label:
        cv.text(c + V(0, 12 * s), label, 34 * s)


def prop_trophy(cv, c, s, lt):
    cv.curve([c + V(-40 * s, -50 * s), c + V(-34 * s, -5 * s), c + V(0, 15 * s), c + V(34 * s, -5 * s),
              c + V(40 * s, -50 * s)], _w(s), INK, closed=True, fill=GOLD, wob=0)
    for sx in (-1, 1):
        cv.curve([c + V(sx * 38 * s, -40 * s), c + V(sx * 60 * s, -30 * s), c + V(sx * 32 * s, -12 * s)], _w(s, 0.8), wob=0)
    cv.line(c + V(0, 15 * s), c + V(0, 40 * s), _w(s))
    cv.rect(c + V(0, 48 * s), 60 * s, 16 * s, 0, 3 * s, _w(s), INK, GOLD)


def prop_heart(cv, c, s, lt, color=PINK):
    cv.heart(c, 40 * s * (1 + 0.08 * math.sin(lt * 10)), _w(s), INK, color)


def prop_star(cv, c, s, lt, color=YELLOW):
    cv.star(c, 42 * s, 5, 0.3 * math.sin(lt * 2), _w(s), INK, color)


def prop_question(cv, c, s, lt):
    cv.text(c + V(0, -6 * s * math.sin(lt * 6)), "?", 120 * s)


def prop_exclamation(cv, c, s, lt):
    cv.text(c + V(0, -6 * s * abs(math.sin(lt * 9))), "!", 120 * s, RED)


def prop_sweat(cv, c, s, lt):
    u = (lt % 0.9) / 0.9
    p = c + V(0, u * 40 * s)
    r = 10 * s * (1 - 0.5 * u)
    cv.curve([p + V(0, -2 * r), p + V(r, 0.1 * r), p + V(0, r), p + V(-r, 0.1 * r)],
             _w(s, 0.6), INK, closed=True, fill=LIGHT_BLUE, wob=0)


def prop_zzz(cv, c, s, lt):
    for i in range(3):
        u = ((lt * 0.7 + i / 3) % 1.0)
        cv.text(c + V(30 * s * u + i * 6 * s, -80 * s * u), "Z", (26 + 22 * u) * s)


def prop_anger(cv, c, s, lt):
    k = 1 + 0.15 * abs(math.sin(lt * 8))
    for a in range(4):
        d = rot(V(1, 0), a * math.pi / 2 + math.pi / 4)
        p = c + d * 14 * s * k
        cv.curve([p + rot(d, 1.2) * 12 * s * k, p, p + rot(d, -1.2) * 12 * s * k], _w(s, 0.9), RED, wob=0)


def prop_music_note(cv, c, s, lt):
    p = c + V(8 * s * math.sin(lt * 3), -10 * s * math.sin(lt * 2))
    cv.ellipse(p, 13 * s, 10 * s, -0.4, _w(s, 0.6), INK, INK, wob=0)
    cv.line(p + V(11 * s, -3 * s), p + V(11 * s, -50 * s), _w(s, 0.7), wob=0)
    cv.curve([p + V(11 * s, -50 * s), p + V(26 * s, -40 * s), p + V(24 * s, -26 * s)], _w(s, 0.7), wob=0)


def prop_chart(cv, c, s, lt, bars=(0.25, 0.45, 0.4, 0.7, 1.0), grow=1.2, color=GREEN):
    grow = (1.2 if grow else 0.001) if isinstance(grow, bool) else float(grow)
    w, h = 260 * s, 200 * s
    o = c + V(-w / 2, h / 2)
    cv.line(o, o + V(0, -h), _w(s))
    cv.line(o, o + V(w, 0), _w(s))
    bw = w / (len(bars) * 1.5 + 0.5)
    tops = []
    for i, b in enumerate(bars):
        g = ease_out(window(lt, i * 0.12, i * 0.12 + grow))
        bh = b * (h - 20 * s) * g
        x = o[0] + bw * (0.5 + i * 1.5) + bw / 2
        if bh > 2:
            cv.rect(V(x, o[1] - bh / 2), bw, bh, 0, 0, _w(s, 0.6), INK, color)
        tops.append(V(x, o[1] - bh - 14 * s))
    if lt > grow * 0.8:
        cv.arrow(tops[0], tops[-1] + V(10 * s, -10 * s), _w(s, 0.9), RED, bend=-0.1, wob=0)


def prop_clock(cv, c, s, lt):
    r = 60 * s
    cv.circle(c, r, _w(s), INK, PAPER)
    cv.line(c, c + rot(V(0, -r * 0.75), lt * 6), _w(s, 0.7), wob=0)
    cv.line(c, c + rot(V(0, -r * 0.5), lt * 0.5), _w(s), wob=0)
    cv.dot(c, 5 * s)


def prop_cloud(cv, c, s, lt):
    blobs = [(-60, 10, 38), (-20, -18, 48), (30, -10, 44), (65, 12, 32), (0, 18, 40)]
    for x, y, r in blobs:
        cv.circle(c + V(x * s, y * s), r * s, _w(s, 0.8), INK, PAPER, wob=0)
    for x, y, r in blobs:
        cv.circle(c + V(x * s, y * s), r * s - _w(s, 0.8) * 0.6, 0, None, PAPER, wob=0)


def prop_sun(cv, c, s, lt):
    r = 50 * s
    for j in range(10):
        d = rot(V(1, 0), j * math.pi / 5 + lt * 0.5)
        cv.line(c + d * r * 1.25, c + d * r * 1.6, _w(s, 0.8), wob=0)
    cv.circle(c, r, _w(s), INK, YELLOW)


def prop_tree(cv, c, s, lt):
    """c = ground point."""
    cv.line(c, c + V(0, -120 * s), _w(s, 1.4))
    top = c + V(0, -170 * s)
    for x, y, r in ((-40, 10, 50), (40, 10, 50), (0, -30, 60)):
        cv.circle(top + V(x * s, y * s), r * s, _w(s), INK, (190, 230, 170), wob=0)


def prop_house(cv, c, s, lt):
    """c = ground point."""
    cv.rect(c + V(0, -80 * s), 200 * s, 160 * s, 0, 0, _w(s), INK, PAPER)
    cv.polygon([c + V(-120 * s, -160 * s), c + V(0, -260 * s), c + V(120 * s, -160 * s)], _w(s), INK, (240, 180, 160))
    cv.rect(c + V(0, -40 * s), 50 * s, 80 * s, 0, 0, _w(s, 0.8), INK, (200, 160, 120))


PROPS = {"lightbulb": prop_lightbulb, "laptop": prop_laptop, "desk": prop_desk, "workstation": prop_workstation,
         "money_bill": prop_money_bill, "coin": prop_coin, "coin_stack": prop_coin_stack,
         "money_bag": prop_money_bag, "phone": prop_phone, "coffee": prop_coffee, "sign": prop_sign,
         "box": prop_box, "trophy": prop_trophy, "heart": prop_heart, "star": prop_star,
         "question": prop_question, "exclamation": prop_exclamation, "sweat": prop_sweat, "zzz": prop_zzz,
         "anger": prop_anger, "music_note": prop_music_note, "chart": prop_chart, "clock": prop_clock,
         "cloud": prop_cloud, "sun": prop_sun, "tree": prop_tree, "house": prop_house}


def prop_chair(cv, c, s, lt, color=PAPER):
    """c = floor point under the seat. Seat height matches the 'sit' pose. Use z=-1 (behind the character)."""
    seat = c[1] - 0.10 * HC * s
    hw = 80 * s
    for sx in (-1, 1):
        cv.line(V(c[0] + sx * hw * 0.85, seat), V(c[0] + sx * hw * 0.85, c[1] + 6 * s), _w(s))
        cv.line(V(c[0] + sx * hw * 0.85, seat), V(c[0] + sx * hw * 0.85, seat - 0.30 * HC * s), _w(s))
    cv.rect(V(c[0], seat - 0.30 * HC * s), hw * 2, 22 * s, 0, 6 * s, _w(s), INK, color)
    cv.rect(V(c[0], seat), hw * 2.1, 14 * s, 0, 4 * s, _w(s), INK, color)


def prop_book(cv, c, s, lt, color=BLUE, title=""):
    """Open book; hold with the 'read' pose (hand='r')."""
    w, h = 70 * s, 85 * s
    for sx in (-1, 1):
        cv.polygon([c, c + V(sx * w, -8 * s), c + V(sx * w, -h), c + V(0, -h + 8 * s)], _w(s), INK, PAPER)
        for j in range(3):
            y = -h + 25 * s + j * 18 * s
            cv.line(c + V(sx * 14 * s, y), c + V(sx * (w - 12 * s), y - 4 * s), _w(s, 0.5), INK, wob=0)
    cv.line(c, c + V(0, -h + 8 * s), _w(s, 1.1), color, wob=0)
    if title:
        cv.text(c + V(0, 22 * s), title, 26 * s, color)


# --- productivity & focus ---
def prop_checklist(cv, c, s, lt, items=4, checked=3, color=GREEN):
    """Clipboard with task lines (or a list of item strings) that sequentially animate checkmarks."""
    labels = [str(x) for x in items] if isinstance(items, (list, tuple)) else None
    n = max(1, min(6, len(labels) if labels is not None else int(items)))
    tw_max = max((cv.text_width(lb, 22 * s) for lb in labels[:n]), default=0.0) if labels else 0.0
    w, h = max(155 * s, tw_max + 78 * s), 200 * s
    cv.rect(c, w, h, 0, 12 * s, _w(s), INK, PAPER)
    cv.rect(c + V(0, -h / 2), 64 * s, 22 * s, 0, 6 * s, _w(s, 0.85), INK, LIGHT_GRAY)
    for i in range(n):
        y = c[1] - h * 0.30 + i * (h * 0.68 / max(1, n - 1 if n > 1 else 1))
        bx = c[0] - w / 2 + 26 * s
        cv.rect(V(bx, y), 22 * s, 22 * s, 0, 4 * s, _w(s, 0.65), INK, PAPER)
        if labels and i < len(labels):
            cv.text(V(bx + 20 * s, y), labels[i], 22 * s, INK, anchor="lm")
        else:
            cv.line(V(bx + 22 * s, y), V(c[0] + w * 0.35, y), _w(s, 0.65), GRAY, wob=0.5)
        is_chk = (bool(checked[i]) if i < len(checked) else False) if isinstance(checked, (list, tuple)) else (
            checked if isinstance(checked, bool) else (i < int(checked)))
        if is_chk:
            u = ease_out(window(lt, 0.2 + i * 0.3, 0.45 + i * 0.3))
            if u > 0.05:
                p0, p1, p2 = V(bx - 6 * s, y), V(bx - 1 * s, y + 7 * s), V(bx + 12 * s, y - 9 * s)
                if u < 0.45:
                    cv.line(p0, lerp(p0, p1, u / 0.45), _w(s, 0.95), color, wob=0)
                else:
                    cv.line(p0, p1, _w(s, 0.95), color, wob=0)
                    cv.line(p1, lerp(p1, p2, (u - 0.45) / 0.55), _w(s, 0.95), color, wob=0)


def prop_hourglass(cv, c, s, lt, color=GOLD):
    """Hourglass with draining top sand, falling stream, and filling bottom sand."""
    w, h = 90 * s, 140 * s
    u = clamp((lt % 3.5) / 3.0)
    top_h = (1 - u) * (h * 0.38)
    bot_h = u * (h * 0.40)
    if top_h > 3 * s:
        tw = w * 0.42 * (top_h / (h * 0.42))
        cv.polygon([c + V(-tw, -top_h), c + V(tw, -top_h), c], 0, None, color)
    if bot_h > 2 * s:
        cv.polygon([c + V(-w * 0.44, h / 2 - 6 * s), c + V(w * 0.44, h / 2 - 6 * s),
                    c + V(0, h / 2 - 6 * s - bot_h)], 0, None, color)
    if u < 0.98:
        cv.line(c, c + V(0, h / 2 - 6 * s), _w(s, 0.45), color, wob=0)
    cv.polygon([c + V(-w / 2, -h / 2), c + V(w / 2, -h / 2), c], _w(s, 0.85), INK, None)
    cv.polygon([c, c + V(w / 2, h / 2), c + V(-w / 2, h / 2)], _w(s, 0.85), INK, None)
    for sy in (-1, 1):
        cv.rect(c + V(0, sy * (h / 2 + 6 * s)), w * 1.18, 12 * s, 0, 3 * s, _w(s, 0.85), INK, (200, 160, 120))


def prop_target(cv, c, s, lt, arrow=True, color=RED):
    """Bullseye target board with an arrow that flies in and vibrates on impact."""
    for r_k, fill in ((72, color), (50, PAPER), (28, color), (11, GOLD)):
        cv.circle(c, r_k * s, _w(s, 0.75), INK, fill)
    if arrow and lt > 0.1:
        u = ease_out(window(lt, 0.1, 0.35))
        vib = math.sin((lt - 0.35) * 42) * math.exp(-max(0.0, lt - 0.35) * 6) * 0.08 if lt > 0.35 else 0.0
        d = rot(norm_v := V(0.78, -0.62), vib)
        tip = lerp(c + norm_v * 140 * s, c, u)
        tail = tip + d * 95 * s
        cv.line(tail, tip, _w(s, 0.85), INK, wob=0)
        for sx in (-1, 1):
            cv.line(tip, tip + rot(d, sx * 0.45) * 18 * s, _w(s, 0.75), INK, wob=0)
            cv.line(tail - d * 10 * s, tail + rot(d, sx * 0.55) * 16 * s, _w(s, 0.65), color, wob=0)


def prop_rocket(cv, c, s, lt, flame=True, color=RED):
    """Hovering/launching rocket with flickering exhaust flames."""
    c = c + V(0, 6 * s * math.sin(lt * 8))
    if flame:
        fk = 1.0 + 0.28 * math.sin(lt * 32)
        cv.polygon([c + V(-18 * s, 52 * s), c + V(0, (52 + 54 * fk) * s), c + V(18 * s, 52 * s)],
                   _w(s, 0.6), INK, ORANGE)
        cv.polygon([c + V(-10 * s, 52 * s), c + V(0, (52 + 32 * fk) * s), c + V(10 * s, 52 * s)],
                   0, None, YELLOW)
    for sx in (-1, 1):
        cv.polygon([c + V(sx * 24 * s, 18 * s), c + V(sx * 52 * s, 56 * s), c + V(sx * 20 * s, 48 * s)],
                   _w(s, 0.8), INK, color)
    cv.curve([c + V(0, -82 * s), c + V(32 * s, -25 * s), c + V(24 * s, 50 * s),
              c + V(-24 * s, 50 * s), c + V(-32 * s, -25 * s)], _w(s), INK, closed=True, fill=PAPER, wob=0.4)
    cv.polygon([c + V(0, -82 * s), c + V(24 * s, -38 * s), c + V(-24 * s, -38 * s)], _w(s, 0.85), INK, color)
    cv.circle(c + V(0, -6 * s), 16 * s, _w(s, 0.75), INK, LIGHT_BLUE)


def prop_battery(cv, c, s, lt, level=1.0, charge=True, color=GREEN):
    """Battery icon that charges up its bars over time."""
    w, h = 140 * s, 74 * s
    cv.rect(c + V(w / 2 + 7 * s, 0), 12 * s, 32 * s, 0, 3 * s, _w(s, 0.75), INK, GRAY)
    cv.rect(c, w, h, 0, 10 * s, _w(s), INK, PAPER)
    u = clamp(lerp(0.2, level, ease_out(window(lt, 0.1, 1.3))) if charge else level)
    col = RED if u < 0.3 else (GOLD if u < 0.65 else color)
    n_bars = 4
    bw = (w - 28 * s) / n_bars
    for i in range(n_bars):
        if u >= (i + 0.3) / n_bars:
            bx = c[0] - w / 2 + 14 * s + bw * (i + 0.5)
            cv.rect(V(bx, c[1]), bw * 0.78, h - 22 * s, 0, 3 * s, _w(s, 0.5), INK, col)
    if charge:
        k = 1 + 0.12 * math.sin(lt * 10)
        cv.polygon([c + V(4 * s * k, -22 * s * k), c + V(-10 * s * k, 2 * s * k), c + V(2 * s * k, 2 * s * k),
                    c + V(-4 * s * k, 22 * s * k), c + V(10 * s * k, -2 * s * k), c + V(-2 * s * k, -2 * s * k)],
                   _w(s, 0.55), INK, YELLOW)


def prop_gear(cv, c, s, lt, color=GRAY):
    """Two interlocking rotating gears (systems / automation / workflow)."""
    def draw_cog(pos, r, teeth, ang, col):
        pts = []
        for i in range(teeth * 4):
            a = ang + i * 2 * math.pi / (teeth * 4)
            rr = r * (1.18 if (i % 4) in (1, 2) else 0.92)
            pts.append(pos + V(math.cos(a) * rr, math.sin(a) * rr))
        cv.polygon(pts, _w(s, 0.75), INK, col)
        cv.circle(pos, r * 0.38, _w(s, 0.7), INK, PAPER)

    draw_cog(c + V(-18 * s, -12 * s), 44 * s, 8, lt * 1.6, color)
    draw_cog(c + V(42 * s, 30 * s), 28 * s, 6, -lt * 2.56 + 0.3, LIGHT_BLUE)


def prop_alarm_clock(cv, c, s, lt, ring=True, color=RED):
    """Twin-bell alarm clock that shakes and rings."""
    ang = 0.12 * math.sin(lt * 36) if ring else 0.0
    p = c + V(0, -4 * s * abs(math.sin(lt * 18)) if ring else 0)
    r = 54 * s
    for sx in (-1, 1):
        cv.line(p + V(sx * 28 * s, r * 0.75), p + V(sx * 38 * s, r * 1.12), _w(s, 0.9))
        bell = p + rot(V(sx * 42 * s, -44 * s), ang)
        cv.circle(bell, 20 * s, _w(s, 0.8), INK, color)
        if ring:
            for k in (1, 2):
                d = V(sx, -0.8)
                cv.curve([bell + d * (18 + k * 9) * s + V(-8 * s, 6 * s),
                          bell + d * (22 + k * 9) * s,
                          bell + d * (18 + k * 9) * s + V(8 * s, -6 * s)], _w(s, 0.55), color, wob=0)
    cv.circle(p, r, _w(s), INK, PAPER)
    cv.line(p, p + rot(V(0, -r * 0.68), lt * 5), _w(s, 0.7), wob=0)
    cv.line(p, p + rot(V(0, -r * 0.45), 1.2 + lt * 0.5), _w(s, 0.9), wob=0)
    cv.dot(p, 5 * s)


# --- making money & finance ---
def prop_piggy_bank(cv, c, s, lt, coin_drop=True, color=PINK):
    """Piggy bank with a gold coin periodically dropping into the top slot."""
    u = (lt % 1.2) / 0.55
    sq = 1.0 - (0.08 * math.sin(math.pi * (u - 1) / 0.35) if 1.0 <= u <= 1.35 and coin_drop else 0.0)
    if coin_drop and u < 1.0:
        prop_coin(cv, c + V(-4 * s, lerp(-95 * s, -36 * s, ease_in(u))), 0.85 * s, lt, flip=lt * 6)
    for lx in (-34, -14, 16, 36):
        cv.rect(c + V(lx * s, 44 * s), 14 * s, 26 * s, 0, 3 * s, _w(s, 0.75), INK, color)
    cv.curve([c + V(-64 * s, -6 * s), c + V(-84 * s, -18 * s), c + V(-74 * s, -30 * s), c + V(-88 * s, -38 * s)],
             _w(s, 0.7), INK, wob=0)
    cv.ellipse(c, 66 * s * (2 - sq), 48 * s * sq, 0, _w(s), INK, color)
    cv.rect(c + V(66 * s, 4 * s), 22 * s, 28 * s, 0, 6 * s, _w(s, 0.8), INK, color)
    cv.polygon([c + V(12 * s, -40 * s), c + V(28 * s, -66 * s), c + V(38 * s, -34 * s)], _w(s, 0.8), INK, color)
    cv.line(c + V(-18 * s, -44 * s * sq), c + V(6 * s, -44 * s * sq), _w(s, 1.1), INK, wob=0)
    cv.dot(c + V(38 * s, -12 * s), 4.5 * s)


def prop_credit_card(cv, c, s, lt, label="CARD", color=LIGHT_BLUE):
    """Floating credit/debit card with gold EMV chip and stripe."""
    ang = 0.06 * math.sin(lt * 2.5)
    p = c + V(0, 5 * s * math.sin(lt * 3))
    w, h = 175 * s, 110 * s
    cv.rect(p, w, h, ang, 12 * s, _w(s), INK, color)
    cv.rect(p + rot(V(0, -26 * s), ang), w * 0.96, 20 * s, ang, 0, _w(s, 0.5), INK, INK)
    cv.rect(p + rot(V(-50 * s, 6 * s), ang), 28 * s, 22 * s, ang, 4 * s, _w(s, 0.65), INK, GOLD)
    cv.line(p + rot(V(-64 * s, 32 * s), ang), p + rot(V(20 * s, 32 * s), ang), _w(s, 0.65), INK, wob=0)
    if label:
        cv.text(p + rot(V(45 * s, 28 * s), ang), label, 24 * s, INK)


def prop_pie_chart(cv, c, s, lt, slices=(0.45, 0.30, 0.25), grow=1.0):
    """Animated circular pie chart whose wedges sweep open."""
    grow = (1.0 if grow else 0.001) if isinstance(grow, bool) else float(grow)
    r = 78 * s
    cols = [GREEN, BLUE, GOLD, ORANGE, PURPLE, PINK]
    u = ease_out(window(lt, 0, grow))
    tot = sum(slices) or 1.0
    a0 = -math.pi / 2
    for i, sl in enumerate(slices):
        span = (sl / tot) * 2 * math.pi * u
        if span > 0.02:
            a1 = a0 + span
            mid = (a0 + a1) / 2
            off = V(math.cos(mid), math.sin(mid)) * (8 * s if i == 0 and u > 0.9 else 0.0)
            pts = [c + off] + [c + off + V(math.cos(a) * r, math.sin(a) * r) for a in np.linspace(a0, a1, 18)]
            cv.polygon(pts, _w(s, 0.8), INK, cols[i % len(cols)])
        a0 += span


def prop_line_chart(cv, c, s, lt, points=(0.15, 0.35, 0.28, 0.65, 0.55, 0.95), grow=1.2, color=GREEN):
    """Stock/growth line chart that draws itself left-to-right with data nodes."""
    grow = (1.2 if grow else 0.001) if isinstance(grow, bool) else float(grow)
    w, h = 260 * s, 190 * s
    o = c + V(-w / 2, h / 2)
    for k in (0.33, 0.66, 1.0):
        cv.line(o + V(0, -h * k), o + V(w, -h * k), _w(s, 0.35), GRID, wob=0)
    cv.line(o, o + V(0, -h), _w(s))
    cv.line(o, o + V(w, 0), _w(s))
    n = len(points)
    if n < 2:
        return
    pts = [o + V((i / (n - 1)) * (w - 20 * s) + 10 * s, -p * (h - 24 * s) - 8 * s) for i, p in enumerate(points)]
    u = ease_out(window(lt, 0, grow))
    pos = u * (n - 1)
    idx = min(n - 2, int(pos))
    frac = pos - idx
    drawn = pts[:idx + 1] + ([lerp(pts[idx], pts[idx + 1], frac)] if u > 0 else [])
    if len(drawn) >= 2:
        cv.stroke(drawn, _w(s, 1.15), color, wob=0.5)
    for i in range(idx + 1):
        cv.circle(pts[i], 6.5 * s, _w(s, 0.6), INK, PAPER, wob=0)


def prop_safe(cv, c, s, lt, open_at=0.6, color=GRAY):
    """Bank safe whose dial spins before the door swings open to reveal gold inside."""
    open_at = (0.6 if open_at else 999.0) if isinstance(open_at, bool) else float(open_at)
    w, h = 140 * s, 140 * s
    for sx in (-1, 1):
        cv.rect(c + V(sx * 46 * s, h / 2 + 8 * s), 22 * s, 14 * s, 0, 2 * s, _w(s, 0.8), INK, INK)
    cv.rect(c, w, h, 0, 10 * s, _w(s), INK, color)
    u = ease_back(window(lt, open_at, open_at + 0.45))
    if u > 0.05:
        cv.rect(c, w * 0.78, h * 0.78, 0, 4 * s, _w(s, 0.7), INK, (50, 55, 65))
        prop_coin_stack(cv, c + V(-18 * s, 28 * s), 0.75 * s, lt, n=4)
        prop_money_bill(cv, c + V(12 * s, 16 * s), 0.55 * s, lt, ang=-0.15)
        dw = w * 0.78 * max(0.18, 1 - 0.7 * u)
        cv.rect(c + V(w * 0.39 + dw / 2, 0), dw, h * 0.82, 0, 6 * s, _w(s, 0.85), INK, color)
    else:
        cv.rect(c, w * 0.78, h * 0.78, 0, 6 * s, _w(s, 0.75), INK, color)
        dial = c + V(8 * s, 0)
        cv.circle(dial, 22 * s, _w(s, 0.75), INK, LIGHT_GRAY)
        for j in range(3):
            a = lt * 8 + j * 2 * math.pi / 3
            cv.line(dial, dial + V(math.cos(a), math.sin(a)) * 18 * s, _w(s, 0.65), INK, wob=0)


def prop_diamond(cv, c, s, lt, color=LIGHT_BLUE):
    """Faceted gemstone with animated sparkle glints."""
    p = c + V(0, 4 * s * math.sin(lt * 3.5))
    top_l, top_r = p + V(-34 * s, -42 * s), p + V(34 * s, -42 * s)
    mid_l, mid_r = p + V(-60 * s, -12 * s), p + V(60 * s, -12 * s)
    bot = p + V(0, 56 * s)
    cv.polygon([top_l, top_r, mid_r, bot, mid_l], _w(s), INK, color)
    cv.line(mid_l, mid_r, _w(s, 0.65), INK, wob=0)
    for xk in (-18, 18):
        cv.line(p + V(xk * s, -42 * s), p + V(xk * 1.4 * s, -12 * s), _w(s, 0.55), INK, wob=0)
        cv.line(p + V(xk * 1.4 * s, -12 * s), bot, _w(s, 0.55), INK, wob=0)
    for i, off in enumerate((V(-58, -42), V(62, -32), V(0, -64))):
        k = max(0.0, math.sin(lt * 5 + i * 2.1))
        if k > 0.2:
            cv.star(p + off * s, 14 * s * k, 4, 0, _w(s, 0.45), INK, YELLOW)


def prop_magnet(cv, c, s, lt, coins=True, color=RED):
    """Horseshoe magnet emitting attraction waves and pulling in coins."""
    p = c + V(2 * s * math.sin(lt * 16), 0)
    r, arm = 42 * s, 45 * s
    pts_out = [p + V(-arm, -r)] + [p + V(-arm + math.cos(a) * r, math.sin(a) * r)
                                   for a in np.linspace(math.pi / 2, 3 * math.pi / 2, 14)] + [p + V(0, -r)]
    cv.stroke(pts_out, _w(s, 2.2), color, wob=0.5)
    for sy in (-1, 1):
        cv.rect(p + V(-6 * s, sy * (r - 12 * s)), 24 * s, 20 * s, 0, 2 * s, _w(s, 0.7), INK, LIGHT_GRAY)
        cv.rect(p + V(-32 * s, sy * (r - 12 * s)), 30 * s, 20 * s, 0, 2 * s, _w(s, 0.7), INK, color)
    for i in range(2):
        u = ((lt * 1.6 + i * 0.5) % 1.0)
        x = 22 * s + (1 - u) * 55 * s
        cv.curve([p + V(x, -26 * s), p + V(x + 12 * s, 0), p + V(x, 26 * s)], _w(s, 0.5), BLUE, wob=0)
        if coins:
            prop_coin(cv, p + V(x + 18 * s, (i * 2 - 1) * 20 * s * u), 0.7 * s, lt, flip=lt * 5 + i)


# --- planning & strategy ---
def prop_calendar(cv, c, s, lt, day="15", mark=True, color=RED):
    """Calendar page with date grid and an animated circle/check around the target day."""
    w, h = 160 * s, 175 * s
    cv.rect(c, w, h, 0, 10 * s, _w(s), INK, PAPER)
    cv.rect(c + V(0, -h / 2 + 24 * s), w, 48 * s, 0, 8 * s, _w(s), INK, color)
    for sx in (-1, 1):
        cv.rect(c + V(sx * 44 * s, -h / 2 - 4 * s), 12 * s, 26 * s, 0, 4 * s, _w(s, 0.75), INK, LIGHT_GRAY)
    for row in range(3):
        for col in range(4):
            cell = c + V((col - 1.5) * 34 * s, (-0.1 + row * 0.32) * h * 0.78)
            cv.rect(cell, 22 * s, 20 * s, 0, 2 * s, _w(s, 0.45), GRAY, LIGHT_GRAY)
    target = c + V(0.5 * 34 * s, 0.22 * h * 0.78)
    if day:
        cv.text(c + V(0, -h / 2 + 24 * s), str(day), 30 * s, PAPER)
    if mark and lt > 0.25:
        u = ease_out(window(lt, 0.25, 0.65))
        pts = ellipse_pts(target, 20 * s, 18 * s, n=28)[:max(2, int(28 * u))]
        cv.stroke(pts, _w(s, 0.95), color, wob=0.6)


def prop_roadmap(cv, c, s, lt, steps=4, color=GREEN):
    """Winding S-curve roadmap with milestone pins popping in along the path."""
    w, h = 250 * s, 170 * s
    ctrl = [c + V(-w * 0.42, h * 0.38), c + V(w * 0.32, h * 0.15),
            c + V(-w * 0.28, -h * 0.15), c + V(w * 0.38, -h * 0.38)]
    path = catmull(ctrl, 16)
    cv.stroke(path, _w(s, 1.8), LIGHT_GRAY, wob=0.5)
    u = ease_out(window(lt, 0.0, 1.2))
    k_end = max(2, int(len(path) * u))
    cv.stroke(path[:k_end], _w(s, 1.1), color, wob=0.5)
    labels = [str(x) for x in steps] if isinstance(steps, (list, tuple)) else None
    n = max(2, min(5, len(labels) if labels is not None else int(steps)))
    for i in range(n):
        idx = int(i * (len(path) - 1) / (n - 1))
        if k_end >= idx + 1:
            p = path[idx]
            cv.circle(p, 10 * s, _w(s, 0.7), INK, GOLD if i == n - 1 else PAPER)
            if labels and i < len(labels):
                cv.text(p + V(0, 22 * s), labels[i], 20 * s, INK)
            if i == n - 1:
                cv.line(p, p + V(0, -34 * s), _w(s, 0.75))
                cv.polygon([p + V(0, -34 * s), p + V(24 * s, -25 * s), p + V(0, -16 * s)], _w(s, 0.65), INK, RED)


def prop_whiteboard(cv, c, s, lt, title="PLAN", color=PAPER):
    """Framed whiteboard where a 3-step flowchart draws itself in."""
    w, h = 270 * s, 180 * s
    cv.rect(c, w + 14 * s, h + 14 * s, 0, 8 * s, _w(s), INK, LIGHT_GRAY)
    cv.rect(c, w, h, 0, 4 * s, _w(s, 0.8), INK, color)
    if title:
        cv.text(c + V(0, -h * 0.34), title, 34 * s, INK)
    b1, b2, b3 = c + V(-82 * s, 18 * s), c + V(0, 18 * s), c + V(82 * s, 18 * s)
    if lt > 0.15:
        cv.rect(b1, 52 * s, 42 * s, 0, 6 * s, _w(s, 0.7), INK, LIGHT_BLUE)
    if lt > 0.45:
        cv.arrow(b1 + V(28 * s, 0), b2 + V(-28 * s, 0), _w(s, 0.7), INK, head=12 * s, wob=0)
        cv.rect(b2, 52 * s, 42 * s, 0, 6 * s, _w(s, 0.7), INK, YELLOW)
    if lt > 0.75:
        cv.arrow(b2 + V(28 * s, 0), b3 + V(-28 * s, 0), _w(s, 0.7), INK, head=12 * s, wob=0)
        cv.star(b3, 26 * s, 5, 0, _w(s, 0.7), INK, GREEN)


def prop_compass(cv, c, s, lt, color=GOLD):
    """Compass with a swinging magnetic needle that settles on target."""
    r = 62 * s
    cv.circle(c, r, _w(s), INK, color)
    cv.circle(c, r * 0.82, _w(s, 0.75), INK, PAPER)
    for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        d = V(math.cos(a), math.sin(a))
        cv.line(c + d * r * 0.64, c + d * r * 0.80, _w(s, 0.65), INK, wob=0)
    ang = -0.6 + 0.95 * math.cos(lt * 5.5) * math.exp(-lt * 1.4)
    d = V(math.cos(ang), math.sin(ang))
    p = V(-d[1], d[0])
    cv.polygon([c + d * r * 0.65, c + p * 9 * s, c - p * 9 * s], _w(s, 0.65), INK, RED)
    cv.polygon([c - d * r * 0.65, c + p * 9 * s, c - p * 9 * s], _w(s, 0.65), INK, GRAY)
    cv.dot(c, 5 * s, INK)


# --- education & ideas ---
def prop_graduation_cap(cv, c, s, lt, color=INK):
    """Mortarboard graduation cap with a swinging gold tassel."""
    w, h = 88 * s, 32 * s
    cv.polygon([c + V(-w * 0.55, 4 * s), c + V(w * 0.55, 4 * s),
                c + V(w * 0.48, 32 * s), c + V(-w * 0.48, 32 * s)], _w(s, 0.85), INK, color)
    cv.polygon([c + V(0, -h), c + V(w, 0), c + V(0, h), c + V(-w, 0)], _w(s), INK, color)
    sway = 10 * s * math.sin(lt * 4.5)
    t_top = c + V(w * 0.55, 6 * s)
    t_bot = t_top + V(sway, 36 * s)
    cv.curve([c, t_top, t_bot], _w(s, 0.75), GOLD, wob=0)
    cv.dot(c, 6 * s, GOLD)
    cv.circle(t_bot, 6 * s, _w(s, 0.5), INK, GOLD)


def prop_brain(cv, c, s, lt, glow=True, color=PINK):
    """Hand-drawn brain with pulsing neural spark lines."""
    blobs = [(-32, -16, 28), (0, -24, 30), (32, -16, 28), (-38, 12, 26), (38, 12, 26), (-14, 22, 28), (16, 22, 28)]
    for x, y, r in blobs:
        cv.circle(c + V(x * s, y * s), r * s, _w(s, 0.85), INK, color, wob=0.5)
    for x, y, r in blobs:
        cv.circle(c + V(x * s, y * s), r * s - _w(s, 0.8), 0, None, color, wob=0.4)
    cv.curve([c + V(-28 * s, -8 * s), c + V(-6 * s, -2 * s), c + V(-18 * s, 14 * s)], _w(s, 0.6), INK)
    cv.curve([c + V(26 * s, -10 * s), c + V(8 * s, 2 * s), c + V(22 * s, 16 * s)], _w(s, 0.6), INK)
    if glow:
        for j in range(6):
            a = j * math.pi / 3 + 0.3
            k = 0.5 + 0.5 * math.sin(lt * 9 + j * 1.7)
            d = V(math.cos(a), math.sin(a))
            cv.line(c + d * 70 * s, c + d * (78 + 18 * k) * s, _w(s, 0.65), GOLD, wob=0)


def prop_certificate(cv, c, s, lt, label="A+", color=GOLD):
    """Framed diploma/certificate with a gleaming ribbon seal."""
    w, h = 180 * s, 130 * s
    cv.rect(c, w, h, 0, 8 * s, _w(s), INK, PAPER)
    cv.rect(c, w - 20 * s, h - 20 * s, 0, 4 * s, _w(s, 0.5), color, None)
    if label:
        cv.text(c + V(-18 * s, -22 * s), label, 42 * s, INK)
    cv.line(c + V(-62 * s, 12 * s), c + V(12 * s, 12 * s), _w(s, 0.6), GRAY, wob=0.4)
    cv.line(c + V(-62 * s, 30 * s), c + V(-4 * s, 30 * s), _w(s, 0.6), GRAY, wob=0.4)
    seal = c + V(46 * s, 22 * s)
    for sx in (-1, 1):
        cv.polygon([seal, seal + V(sx * 16 * s, 38 * s), seal + V(sx * 4 * s, 32 * s)], _w(s, 0.6), INK, RED)
    cv.star(seal, 22 * s * (1 + 0.08 * math.sin(lt * 6)), 8, lt * 0.8, _w(s, 0.65), INK, color)


def prop_globe(cv, c, s, lt, color=LIGHT_BLUE):
    """Rotating Earth globe on a desk stand."""
    r = 56 * s
    cv.circle(c, r, _w(s), INK, color)
    shift = ((lt * 0.45) % 1.0) * 2 - 1
    cv.ellipse(c + V(shift * 22 * s, -12 * s), 22 * s * (1 - 0.4 * abs(shift)), 16 * s, 0.2,
               _w(s, 0.55), INK, LIGHT_GREEN)
    cv.ellipse(c + V(-shift * 20 * s, 16 * s), 18 * s * (1 - 0.4 * abs(shift)), 14 * s, -0.3,
               _w(s, 0.55), INK, LIGHT_GREEN)
    cv.line(c + V(-r, 0), c + V(r, 0), _w(s, 0.5), INK, wob=0)
    Arc = [c + V(math.cos(a) * (r + 12 * s), math.sin(a) * (r + 12 * s))
           for a in np.linspace(-math.pi * 0.6, math.pi * 0.6, 18)]
    cv.stroke(Arc, _w(s, 0.9), GRAY, wob=0.3)
    cv.line(c + V(0, r + 12 * s), c + V(0, r + 34 * s), _w(s))
    cv.rect(c + V(0, r + 38 * s), 76 * s, 12 * s, 0, 4 * s, _w(s, 0.85), INK, GRAY)


# --- YouTube & content creation ---
def prop_youtube_play(cv, c, s, lt, subs="", color=RED):
    """YouTube Play button plaque with subtle pulse and optional subscriber count."""
    k = 1.0 + 0.04 * math.sin(lt * 5)
    w, h = 168 * s * k, 116 * s * k
    cv.rect(c, w, h, 0, 26 * s * k, _w(s), INK, color)
    tri = [c + V(-20 * s * k, -26 * s * k), c + V(28 * s * k, 0), c + V(-20 * s * k, 26 * s * k)]
    cv.polygon(tri, _w(s, 0.85), INK, PAPER)
    if subs:
        cv.rect(c + V(0, h / 2 + 28 * s), 140 * s, 36 * s, 0, 8 * s, _w(s, 0.7), INK, GOLD)
        cv.text(c + V(0, h / 2 + 28 * s), str(subs), 26 * s, INK)


def prop_subscribe_btn(cv, c, s, lt, clicked_at=0.6, text="SUBSCRIBE"):
    """YouTube Subscribe button + bell that animates a click and turns Subscribed."""
    clicked_at = (0.6 if clicked_at else 999.0) if isinstance(clicked_at, bool) else float(clicked_at)
    clicked = lt >= clicked_at
    squash = 1.0 - (0.14 * math.sin(math.pi * (lt - clicked_at) / 0.18) if 0 <= lt - clicked_at < 0.18 else 0.0)
    w, h = 210 * s * squash, 68 * s * squash
    cv.rect(c, w, h, 0, 14 * s, _w(s), INK, LIGHT_GRAY if clicked else RED)
    cv.text(c, "SUBSCRIBED" if clicked else text, 30 * s * squash, INK if clicked else PAPER)
    bell = c + V(w / 2 + 36 * s, 0)
    ang = 0.22 * math.sin((lt - clicked_at) * 30) * math.exp(-max(0.0, lt - clicked_at) * 2.5) if clicked else 0.0
    cv.polygon([bell + rot(V(-16 * s, 14 * s), ang), bell + rot(V(-12 * s, -12 * s), ang),
                bell + rot(V(0, -20 * s), ang), bell + rot(V(12 * s, -12 * s), ang),
                bell + rot(V(16 * s, 14 * s), ang)], _w(s, 0.75), INK, GOLD if clicked else PAPER)
    cv.dot(bell + V(0, 18 * s), 5 * s, INK)
    if clicked and lt - clicked_at < 0.55:
        u = (lt - clicked_at) / 0.55
        for j in range(6):
            d = rot(V(1, 0), j * math.pi / 3)
            cv.line(bell + d * (22 + 12 * u) * s, bell + d * (32 + 20 * u) * s, _w(s, 0.6), GOLD, wob=0)


def prop_camera_rec(cv, c, s, lt, rec=True):
    """Video camera on a tripod with a blinking red REC indicator."""
    for sx in (-0.7, 0.0, 0.7):
        cv.line(c + V(0, 32 * s), c + V(sx * 54 * s, 105 * s), _w(s, 0.85))
    cv.rect(c, 108 * s, 68 * s, 0, 10 * s, _w(s), INK, (70, 75, 85))
    cv.polygon([c + V(54 * s, -14 * s), c + V(90 * s, -28 * s), c + V(90 * s, 28 * s), c + V(54 * s, 14 * s)],
               _w(s, 0.85), INK, LIGHT_GRAY)
    cv.circle(c + V(-12 * s, 0), 18 * s, _w(s, 0.7), INK, LIGHT_BLUE)
    if rec and math.sin(lt * 8) > -0.2:
        cv.dot(c + V(-34 * s, -20 * s), 7 * s, RED)
        cv.text(c + V(16 * s, -20 * s), "REC", 20 * s, RED)


def prop_microphone(cv, c, s, lt, waves=True, color=GRAY):
    """Studio podcast/YouTube microphone with radiating sound waves."""
    head = c + V(0, -24 * s)
    cv.ellipse(head, 24 * s, 34 * s, 0, _w(s), INK, color)
    for yk in (-12, 0, 12):
        cv.line(head + V(-18 * s, yk * s), head + V(18 * s, yk * s), _w(s, 0.5), INK, wob=0)
    cv.curve([head + V(-32 * s, -4 * s), head + V(-30 * s, 40 * s), head + V(0, 46 * s),
              head + V(30 * s, 40 * s), head + V(32 * s, -4 * s)], _w(s, 0.85), INK, wob=0)
    cv.line(c + V(0, 22 * s), c + V(0, 62 * s), _w(s))
    cv.rect(c + V(0, 66 * s), 68 * s, 12 * s, 0, 4 * s, _w(s, 0.85), INK, INK)
    if waves:
        for sx in (-1, 1):
            for k in (1, 2):
                u = ((lt * 1.8 + k * 0.4) % 1.0)
                rx = (38 + u * 28) * s * sx
                cv.curve([head + V(rx, -20 * s), head + V(rx + sx * 10 * s, 0), head + V(rx, 20 * s)],
                         _w(s, 0.55 * (1 - 0.5 * u)), BLUE, wob=0)


def prop_video_player(cv, c, s, lt, progress=0.85, likes=True, color=RED):
    """YouTube video player window with advancing red scrub bar and floating likes."""
    w, h = 270 * s, 168 * s
    cv.rect(c, w, h, 0, 12 * s, _w(s), INK, PAPER)
    cv.rect(c + V(0, -12 * s), w - 24 * s, h - 48 * s, 0, 6 * s, _w(s, 0.7), INK, LIGHT_GRAY)
    cv.polygon([c + V(-14 * s, -28 * s), c + V(22 * s, -12 * s), c + V(-14 * s, 4 * s)], _w(s, 0.75), INK, color)
    y_bar = c[1] + h / 2 - 22 * s
    x0, x1 = c[0] - w / 2 + 18 * s, c[0] + w / 2 - 18 * s
    u = clamp(lerp(0.08, progress, ease_out(window(lt, 0, 1.5))))
    xm = lerp(x0, x1, u)
    cv.line(V(x0, y_bar), V(x1, y_bar), _w(s, 0.7), GRAY, wob=0)
    cv.line(V(x0, y_bar), V(xm, y_bar), _w(s, 0.95), color, wob=0)
    cv.dot(V(xm, y_bar), 7 * s, color)
    if likes:
        for i in range(3):
            a = ((lt * 0.9 + i / 3) % 1.0)
            p = c + V(w * 0.36 + 10 * s * math.sin(a * 5 + i), h * 0.25 - a * 90 * s)
            cv.heart(p, 14 * s * (1 - 0.4 * a), _w(s, 0.45), INK, PINK if i % 2 == 0 else RED)


def prop_megaphone(cv, c, s, lt, waves=True, color=ORANGE):
    """Megaphone / bullhorn with animated sound burst lines."""
    k = 1.0 + 0.04 * math.sin(lt * 10)
    cv.rect(c + V(-26 * s, 26 * s), 14 * s, 34 * s, 0.3, 3 * s, _w(s, 0.8), INK, GRAY)
    cv.polygon([c + V(-42 * s, 8 * s), c + V(46 * s * k, -38 * s * k),
                c + V(46 * s * k, 28 * s * k), c + V(-42 * s, -8 * s)], _w(s), INK, color)
    cv.ellipse(c + V(46 * s * k, -5 * s * k), 10 * s, 33 * s * k, 0, _w(s, 0.8), INK, PAPER)
    if waves:
        for j, a in enumerate((-0.35, 0.0, 0.35)):
            d = rot(V(1, -0.08), a)
            u = 0.5 + 0.5 * math.sin(lt * 12 + j)
            cv.line(c + V(58 * s, -5 * s) + d * 10 * s, c + V(58 * s, -5 * s) + d * (28 + 18 * u) * s,
                    _w(s, 0.75), color, wob=0)


PROPS.update(
    chair=prop_chair, book=prop_book,
    checklist=prop_checklist, hourglass=prop_hourglass, target=prop_target, rocket=prop_rocket,
    battery=prop_battery, gear=prop_gear, alarm_clock=prop_alarm_clock,
    piggy_bank=prop_piggy_bank, credit_card=prop_credit_card, pie_chart=prop_pie_chart,
    line_chart=prop_line_chart, safe=prop_safe, diamond=prop_diamond, magnet=prop_magnet,
    calendar=prop_calendar, roadmap=prop_roadmap, whiteboard=prop_whiteboard, compass=prop_compass,
    graduation_cap=prop_graduation_cap, brain=prop_brain, certificate=prop_certificate, globe=prop_globe,
    youtube_play=prop_youtube_play, subscribe_btn=prop_subscribe_btn, camera_rec=prop_camera_rec,
    microphone=prop_microphone, video_player=prop_video_player, megaphone=prop_megaphone,
)

EFFECT_KINDS = ["money_rain", "confetti", "coins_burst", "burst", "sparkles", "hearts", "speed_lines",
                "smoke", "shake", "flash", "likes_burst", "fireworks"]
CONFETTI_COLORS = [RED, BLUE, GOLD, GREEN, PINK, PURPLE, ORANGE]


# =============================================================== sound effects
# All sounds are synthesized with numpy (mono float arrays at SR). Use scene.sound(t, name) or rely on
# the automatic sounds that props, effects, text, speech, counters, jumps, walks, show/hide trigger.
SR = 44100
SFX_LEVEL = 0.55                 # master level of sound effects


def _tt(dur):
    return np.arange(max(1, int(dur * SR))) / SR


def _env(n, attack=0.003, decay=0.2):
    t = np.arange(n) / SR
    return np.clip(t / max(attack, 1e-4), 0, 1) * np.exp(-t / max(decay, 1e-4))


def _oscf(f, wave="sine"):
    """Oscillator from a per-sample frequency array."""
    ph = 2 * np.pi * np.cumsum(f) / SR
    if wave == "square":
        return np.sign(np.sin(ph)) * 0.55
    if wave == "saw":
        return (2 * ((ph / (2 * np.pi)) % 1) - 1) * 0.6
    if wave == "tri":
        return 2 * np.abs(2 * ((ph / (2 * np.pi)) % 1) - 1) - 1
    return np.sin(ph)


def _osc(f0, dur, f1=None, wave="sine", curve=1.0):
    n = max(1, int(dur * SR))
    u = np.linspace(0, 1, n) ** curve
    return _oscf(f0 + ((f0 if f1 is None else f1) - f0) * u, wave)


def _noise(dur, lo=0.0, hi=SR / 2, seed=0):
    n = max(2, int(dur * SR))
    x = np.random.default_rng(seed).standard_normal(n)
    X = np.fft.rfft(x)
    fr = np.fft.rfftfreq(n, 1 / SR)
    X[(fr < lo) | (fr > hi)] = 0
    y = np.fft.irfft(X, n)
    return y / (np.abs(y).max() + 1e-9)


def _bell(f, dur=0.8, partials=((1, 1, 1), (2.0, 0.5, 0.6), (3.0, 0.25, 0.4), (4.2, 0.15, 0.3))):
    t = _tt(dur)
    y = np.zeros_like(t)
    for m, a, dk in partials:
        y += a * np.sin(2 * np.pi * f * m * t) * np.exp(-t / (dur * dk * 0.45))
    return y * np.clip(t / 0.002, 0, 1)


def _seq(parts):
    """Mix [(offset_seconds, array), ...] into one array."""
    n = max(int(o * SR) + len(a) for o, a in parts)
    y = np.zeros(n)
    for o, a in parts:
        i = int(o * SR)
        y[i:i + len(a)] += a
    return y


def _midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def _fadein_out(y, a=0.01, b=0.05):
    n = len(y)
    env = np.ones(n)
    na, nb = min(n, int(a * SR)), min(n, int(b * SR))
    if na:
        env[:na] = np.linspace(0, 1, na)
    if nb:
        env[n - nb:] *= np.linspace(1, 0, nb)
    return y * env


def sfx_pop():
    y = _osc(900, 0.1, 240, curve=0.5)
    return y * _env(len(y), 0.001, 0.03)


def sfx_bubble():
    y = _osc(380, 0.13, 950, curve=0.7)
    return y * _env(len(y), 0.002, 0.045)


def sfx_boing():
    t = _tt(0.45)
    u = t / t[-1]
    f = 170 + 330 * u ** 0.6 + 45 * np.sin(2 * np.pi * 13 * t) * (1 - u)
    return _oscf(f, "tri") * _env(len(t), 0.003, 0.18)


def sfx_whoosh(dur=0.4):
    u = np.linspace(0, 1, max(2, int(dur * SR)))
    y = _noise(dur, 200, 1300, 1) * (1 - u) + _noise(dur, 1300, 6500, 2) * u
    return y * np.sin(np.pi * u) ** 2


def sfx_swish():
    y = _noise(0.18, 2500, 9000, 3)
    return y * np.sin(np.pi * np.linspace(0, 1, len(y))) ** 1.5 * 0.8


def sfx_ding():
    return _bell(_midi(88), 1.0)


def sfx_chime():
    return _seq([(0, _bell(_midi(84), 0.7)), (0.09, _bell(_midi(88), 0.7)), (0.18, _bell(_midi(91), 0.9))])


def sfx_coin():
    a = _osc(988, 0.07, wave="square")
    b = _osc(1319, 0.32, wave="square")
    return _seq([(0, a * _env(len(a), 0.001, 0.5)), (0.07, b * _env(len(b), 0.001, 0.11))])


def sfx_coins():
    r = random.Random(5)
    metal = ((1, 1, 1), (1.5, 0.4, 0.6), (2.7, 0.3, 0.5))
    return _seq([(r.uniform(0, 0.45), _bell(r.uniform(2400, 4200), 0.25, metal) * r.uniform(0.5, 1))
                 for _ in range(8)])


def sfx_cha_ching():
    k1 = _noise(0.04, 1000, 8000, 4)
    k2 = _noise(0.07, 300, 3000, 5)
    metal = ((1, 1, 1), (1.34, 0.7, 0.8), (2.1, 0.5, 0.6), (2.9, 0.3, 0.4))
    return _seq([(0, k1 * _env(len(k1), 0.001, 0.01)), (0.03, 0.7 * k2 * _env(len(k2), 0.001, 0.02)),
                 (0.13, _bell(2093, 1.0, metal)), (0.15, 0.6 * _bell(2637, 0.9, metal))])


def sfx_sparkle():
    r = random.Random(6)
    parts = []
    for _ in range(9):
        p = _osc(r.uniform(2500, 5200), 0.16)
        parts.append((r.uniform(0, 0.5), p * _env(len(p), 0.001, 0.05) * r.uniform(0.4, 1)))
    return _seq(parts)


def sfx_magic():
    notes = [72, 74, 76, 79, 81, 84, 86, 88, 91, 93, 96]
    return _seq([(i * 0.035, _bell(_midi(n), 0.5) * 0.7) for i, n in enumerate(notes)])


def sfx_boom():
    a = _osc(90, 0.8, 35, curve=0.4)
    b = _noise(0.8, 20, 400, 7)
    return a * _env(len(a), 0.002, 0.25) + 0.6 * b * _env(len(b), 0.001, 0.15)


def sfx_thud():
    a = _osc(140, 0.18, 55)
    b = _noise(0.18, 50, 800, 8)
    return a * _env(len(a), 0.001, 0.05) + 0.3 * b * _env(len(b), 0.001, 0.02)


def sfx_impact():
    c = _noise(0.08, 1200, 6000, 9)
    return _seq([(0, sfx_thud()), (0, 0.5 * c * _env(len(c), 0.0005, 0.015))])


def sfx_punch():
    c = _noise(0.12, 300, 3000, 10)
    return _seq([(0, sfx_thud() * 1.2), (0, 0.8 * c * _env(len(c), 0.0005, 0.03))])


def sfx_poof():
    y = _noise(0.35, 100, 1500, 11)
    return y * _env(len(y), 0.01, 0.1)


def sfx_click():
    y = _noise(0.012, 1000, 10000, 12)
    return y * _env(len(y), 0.0005, 0.003)


def sfx_tick():
    y = _osc(2200, 0.03)
    return y * _env(len(y), 0.0005, 0.008)


def sfx_blip():
    y = _osc(620, 0.05, wave="square")
    return y * _env(len(y), 0.002, 0.03)


def sfx_type():
    y = _noise(0.02, 1500, 7000, 13)
    return y * _env(len(y), 0.0005, 0.005)


def sfx_step():
    y = _noise(0.07, 60, 600, 14)
    return y * _env(len(y), 0.002, 0.02)


def sfx_zap():
    y = _osc(2400, 0.25, 150, wave="saw", curve=0.5)
    return y * _env(len(y), 0.001, 0.08)


def sfx_rumble(dur=0.7):
    t = _tt(dur)
    u = t / max(t[-1], 1e-6)
    return _noise(dur, 20, 180, 15) * np.sin(np.pi * u) * (0.7 + 0.3 * np.sin(2 * np.pi * 9 * t))


def sfx_success():
    parts = []
    for i, n in enumerate((72, 76, 79, 84)):
        d = 0.5 if i == 3 else 0.14
        p = _osc(_midi(n), d, wave="tri")
        parts.append((i * 0.1, p * _env(len(p), 0.003, d * 0.5)))
    parts.append((0.3, 0.5 * _bell(_midi(96), 0.8)))
    return _seq(parts)


def sfx_fail():
    parts = []
    for i, n in enumerate((67, 66, 65, 64)):
        d = 0.75 if i == 3 else 0.28
        t = _tt(d)
        f = _midi(n - 12) * (1 + 0.012 * np.sin(2 * np.pi * 6 * t) * (i == 3))
        p = _oscf(f, "saw") * 0.7 + _oscf(f * 2, "sine") * 0.2
        parts.append((i * 0.3, _fadein_out(p, 0.01, 0.08)))
    return _seq(parts)


def sfx_alert():
    a = _osc(880, 0.08, wave="square")
    a = _fadein_out(a, 0.002, 0.01)
    return _seq([(0, a), (0.12, a)])


def sfx_question():
    y = _osc(350, 0.28, 760, curve=2.0)
    return y * _env(len(y), 0.005, 0.15)


def sfx_heart():
    soft = ((1, 1, 1), (2, 0.2, 0.5))
    return _seq([(0, _bell(_midi(79), 0.6, soft)), (0.13, _bell(_midi(86), 0.8, soft))])


def sfx_drop():
    w = _osc(1500, 0.24, 450)
    return _seq([(0, 0.35 * _fadein_out(w, 0.01, 0.03)), (0.24, sfx_thud()), (0.5, 0.4 * sfx_thud())])


def sfx_notify():
    return _seq([(0, _bell(_midi(88), 0.3)), (0.11, _bell(_midi(93), 0.5))])


def sfx_party():
    n = int(0.7 * SR)
    r = np.random.default_rng(16)
    crackle = r.standard_normal(n) * (r.random(n) < 0.01) * np.exp(-np.arange(n) / SR / 0.3)
    return _seq([(0, sfx_pop() * 1.2), (0, 0.6 * _noise(0.2, 300, 3000, 17) * _env(int(0.2 * SR), 0.001, 0.05)),
                 (0.04, crackle * 1.5)])


def sfx_rise():
    y = _osc(300, 0.6, 1000, wave="tri", curve=1.5)
    return _fadein_out(y, 0.02, 0.15) * 0.6


def sfx_snore():
    t = _tt(0.9)
    return _noise(0.9, 80, 500, 18) * np.sin(np.pi * t / t[-1]) ** 2 * 0.6


SOUNDS = {name[4:]: fn for name, fn in globals().items() if name.startswith("sfx_") and callable(fn)}
_SOUND_CACHE = {}

# automatic sound for each prop / effect when it appears ("" = silent)
PROP_SFX = {"lightbulb": "ding", "money_bill": "swish", "coin": "coin", "coin_stack": "coins",
            "money_bag": "cha_ching", "phone": "notify", "trophy": "success", "heart": "heart",
            "star": "sparkle", "question": "question", "exclamation": "alert", "sweat": "blip",
            "zzz": "snore", "anger": "thud", "music_note": "chime", "chart": "rise", "clock": "tick",
            "sun": "chime", "cloud": "whoosh", "book": "swish",
            "checklist": "ding", "hourglass": "tick", "target": "impact", "rocket": "whoosh",
            "battery": "zap", "gear": "tick", "alarm_clock": "alert",
            "piggy_bank": "coin", "credit_card": "swish", "pie_chart": "pop", "line_chart": "rise",
            "safe": "cha_ching", "diamond": "sparkle", "magnet": "coins",
            "calendar": "pop", "roadmap": "rise", "whiteboard": "swish", "compass": "chime",
            "graduation_cap": "success", "brain": "magic", "certificate": "success", "globe": "chime",
            "youtube_play": "notify", "subscribe_btn": "ding", "camera_rec": "notify",
            "microphone": "pop", "video_player": "notify", "megaphone": "alert"}
EFFECT_SFX = {"money_rain": "cha_ching", "confetti": "party", "coins_burst": "coins", "burst": "impact",
              "sparkles": "sparkle", "hearts": "heart", "speed_lines": "whoosh", "smoke": "poof",
              "shake": "rumble", "flash": "zap", "likes_burst": "notify", "fireworks": "party"}


def get_sound(name, **kw):
    """Return the mono float array of a named sound (cached)."""
    if name not in SOUNDS:
        raise ValueError(f"Unknown sound '{name}'. Available: {sorted(SOUNDS)}")
    key = (name, tuple(sorted(kw.items())))
    if key not in _SOUND_CACHE:
        y = np.asarray(SOUNDS[name](**kw), float)
        _SOUND_CACHE[key] = y / (np.abs(y).max() + 1e-9)
    return _SOUND_CACHE[key]


def load_wav(path):
    """Load a 16-bit PCM WAV file as a mono float array at SR."""
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported")
    y = np.frombuffer(raw, "<i2").astype(float).reshape(-1, ch).mean(1) / 32768.0
    if sr != SR:
        y = np.interp(np.arange(0, len(y) - 1, sr / SR), np.arange(len(y)), y)
    return y


def write_wav(path, stereo):
    data = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


MUSIC_STYLES = {   # chord roots (midi) + chord quality per bar, bpm
    "upbeat": dict(bpm=112, chords=[(60, "maj"), (67, "maj"), (69, "min"), (65, "maj")]),
    "chill":  dict(bpm=84,  chords=[(69, "min"), (65, "maj"), (60, "maj"), (67, "maj")]),
    "tense":  dict(bpm=100, chords=[(57, "min"), (57, "min"), (62, "min"), (64, "maj")]),
    "happy":  dict(bpm=124, chords=[(60, "maj"), (65, "maj"), (67, "maj"), (60, "maj")]),
}


def make_music(dur, style="upbeat", bpm=None):
    """Procedural background loop: kick, hi-hat, bass and chord plucks."""
    st = MUSIC_STYLES[style]
    bpm = bpm or st["bpm"]
    beat = 60.0 / bpm
    y = np.zeros(int(dur * SR) + SR)
    kick = _osc(110, 0.25, 40, curve=0.5)
    kick *= _env(len(kick), 0.001, 0.08)
    hat = _noise(0.05, 6000, 14000, 19)
    hat *= _env(len(hat), 0.0005, 0.012)

    def put(t, a, g):
        i = int(t * SR)
        if i < len(y):
            seg = a[:len(y) - i]
            y[i:i + len(seg)] += seg * g

    nb = int(dur / beat) + 1
    for b in range(nb):
        t = b * beat
        root, q = st["chords"][(b // 4) % len(st["chords"])]
        third = 3 if q == "min" else 4
        if b % 2 == 0:
            put(t, kick, 0.9)
        put(t + beat / 2, hat, 0.25)
        put(t, hat, 0.12)
        bass = _osc(_midi(root - 24), beat * 0.9, wave="tri")
        put(t, bass * _env(len(bass), 0.005, beat * 0.5), 0.45)
        chord = sum(_osc(_midi(root + k), beat * 0.6, wave="tri") for k in (0, third, 7, 12))
        put(t + beat / 2, chord * _env(len(chord), 0.004, beat * 0.25), 0.12)
    return _fadein_out(y[:int(dur * SR)], 0.5, 1.0)


def mux_audio(video, wav, out):
    """Combine a silent MP4 and a WAV into out (AAC audio) using the ffmpeg bundled with imageio-ffmpeg."""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([exe, "-y", "-loglevel", "error", "-i", video, "-i", wav, "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-shortest", out], check=True)


# =============================================================== scene
class Ctx:
    """Passed to custom draw functions: g.cv (Canvas), g.t (time), g.lt (local time), g.u (0..1 progress),
    g.dur, g.scene, g.char(name), g.pt(x, y) -> design point from normalized coords."""

    def __init__(self, scene, cv, t, t0, t1):
        self.scene, self.cv, self.t, self.t0, self.t1 = scene, cv, t, t0, t1
        self.lt = t - t0
        self.dur = (t1 - t0) if t1 != float("inf") else scene.duration - t0
        self.u = clamp(self.lt / self.dur) if self.dur > 0 else 1.0

    def char(self, name):
        return self.scene.chars[name]

    @staticmethod
    def pt(x, y):
        return V(x * BW, y * BH)


class Scene:
    def __init__(self, duration=5.0, fps=30, width=1920, background="grid", bg_color=PAPER, seed=1,
                 sound=True, sfx_volume=1.0):
        """background: 'grid' | 'white' | 'dots' | (r,g,b) | callable(cv, t).
        sound: automatic sound effects on/off (manual scene.sound / scene.music always play).
        sfx_volume: master volume of all sound effects."""
        self.duration, self.fps, self.width = float(duration), fps, width
        self.background, self.bg_color, self.seed = background, bg_color, seed
        self.items, self.chars = [], {}
        self.cam_keys, self.shakes, self.flashes, self.fades = [], [], [], []
        self._order = 0
        self.sound_on, self.sfx_volume = sound, sfx_volume
        self.sfx, self.music_tracks = [], []

    # ------------------------------------------------------------ sound
    @staticmethod
    def _pick(sfx, auto):
        """sfx=None -> auto sound, False/'' -> silent, 'name' -> that sound."""
        if sfx is False or sfx == "" or sfx == "none":
            return None
        return sfx or auto

    def _pan(self, x=None, at=None):
        if at is not None and at in self.chars:
            x = self.chars[at].x0 / BW
        return 0.0 if x is None else clamp((x - 0.5) * 1.2, -0.8, 0.8)

    def _sfx(self, t, name, volume=1.0, pan=0.0, pitch=1.0, **kw):
        if self.sound_on and name:
            if name not in SOUNDS:
                raise ValueError(f"Unknown sound '{name}'. Available: {sorted(SOUNDS)}")
            self.sfx.append((t, name, volume, pan, pitch, kw))

    def sound(self, t, name, volume=1.0, pan=0.0, pitch=1.0, **kw):
        """Play a synthesized sound at time t. name: SOUNDS. pan -1 left .. 1 right, pitch 0.5..2.
        Returns the end time. Some sounds take kw, e.g. sound(t, 'whoosh', dur=0.6), sound(t, 'rumble', dur=1)."""
        if name not in SOUNDS:
            raise ValueError(f"Unknown sound '{name}'. Available: {sorted(SOUNDS)}")
        self.sfx.append((t, name, volume, pan, pitch, kw))
        return t + len(get_sound(name, **kw)) / SR / pitch

    def sound_file(self, t, path, volume=1.0, pan=0.0):
        """Play your own 16-bit WAV file at time t. Returns the end time."""
        y = load_wav(path)
        self.sfx.append((t, y, volume, pan, 1.0, {}))
        return t + len(y) / SR

    def music(self, style="upbeat", volume=0.35, t0=0.0, t1=None, bpm=None, path=None):
        """Background music. style: MUSIC_STYLES ('upbeat', 'chill', 'tense', 'happy') or path= a WAV file."""
        t1 = self.duration if t1 is None else t1
        y = load_wav(path)[:int((t1 - t0) * SR)] if path else make_music(t1 - t0, style, bpm)
        self.music_tracks.append((t0, _fadein_out(y, 0.3, 0.8), volume))

    def render_audio(self):
        """Mix all sound events + music into a stereo float array (n, 2)."""
        n = int(self.duration * SR) + 1
        L, R = np.zeros(n), np.zeros(n)
        for t, name, vol, pan, pitch, kw in sorted(self.sfx, key=lambda e: e[0]):
            y = name if isinstance(name, np.ndarray) else get_sound(name, **kw)
            if pitch != 1.0:
                y = np.interp(np.arange(0, len(y) - 1, pitch), np.arange(len(y)), y)
            i = int(max(0.0, t) * SR)
            if i >= n:
                continue
            y = y[:n - i] * vol * SFX_LEVEL * self.sfx_volume
            a = (clamp(pan, -1, 1) + 1) * math.pi / 4
            L[i:i + len(y)] += y * math.cos(a) * 1.414
            R[i:i + len(y)] += y * math.sin(a) * 1.414
        for t0, y, vol in self.music_tracks:
            i = int(t0 * SR)
            if i < n:
                y = y[:n - i] * vol
                L[i:i + len(y)] += y
                R[i:i + len(y)] += y
        st = np.stack([L, R], 1)
        return np.tanh(st * 1.2) / np.tanh(1.2)            # soft limiter

    def has_audio(self):
        return bool(self.sfx or self.music_tracks)

    # ------------------------------------------------------------ building
    def add(self, fn, t0=0.0, t1=None, z=20, screen=False):
        """Register a custom drawing function fn(g: Ctx) active between t0 and t1."""
        t1 = float("inf") if t1 is None else t1
        self._order += 1
        self.items.append((z, self._order, t0, t1, screen, lambda cv, t: fn(Ctx(self, cv, t, t0, t1))))
        return fn

    def layer(self, t0=0.0, t1=None, z=20, screen=False):
        """Decorator version of add(): @scene.layer(1, 3, z=5) def my_fx(g): ..."""
        def deco(fn):
            return self.add(fn, t0, t1, z, screen)
        return deco

    def _raw(self, draw, t0, t1, z, screen):
        self._order += 1
        self.items.append((z, self._order, t0, t1, screen, draw))

    def add_character(self, name, x=0.5, ground=0.86, scale=1.0, color=INK, facing=0, z=0, visible=True):
        """x: 0..1 across the screen, ground: 0..1 down the screen (feet line)."""
        c = Character(self, name, x, ground, scale, color, facing, z, visible)
        self.chars[name] = c
        self._raw(c.draw, 0, float("inf"), z, False)
        return c

    def resolve(self, t, at=None, anchor="head", offset=(0.0, 0.0), x=0.5, y=0.5):
        """Point from a character anchor (at='bob') or normalized (x, y)."""
        if at is not None:
            return self.chars[at].anchor(t, anchor, offset)
        return V(x * BW, y * BH)

    def prop(self, name, t0, t1, x=0.5, y=0.5, at=None, anchor="above_head", offset=(0.0, 0.0),
             scale=1.0, anim="pop", z=5, sfx=None, volume=1.0, **kw):
        """Show a prop. Position: normalized x,y OR attached to character `at` + anchor.
        anim: 'pop' | 'drop' | 'grow' | 'slide_left' | 'slide_right' | 'float' | 'bounce' | 'pulse' | 'none'.
        sfx: None = automatic appear sound, False = silent, or a SOUNDS name."""
        if name not in PROPS:
            raise ValueError(f"Unknown prop '{name}'. Available: {sorted(PROPS)}")
        auto = {"drop": "drop", "slide_left": "whoosh", "slide_right": "whoosh",
                "none": None}.get(anim, PROP_SFX.get(name, "pop"))
        self._sfx(t0, self._pick(sfx, auto), volume, self._pan(None if at else x, at))

        def draw(cv, t):
            c = self.resolve(t, at, anchor, offset, x, y)
            lt, s = t - t0, scale
            if anim == "pop":
                s *= pop_scale(t, t0, t1)
            elif anim == "grow":
                s *= ease_out(window(t, t0, t0 + 0.5)) * (1 - ease_in(window(t, t1 - 0.2, t1)))
            elif anim == "drop":
                c = c + V(0, -(1 - ease_bounce(window(t, t0, t0 + 0.7))) * 700)
            elif anim in ("slide_left", "slide_right"):
                sgn = -1 if anim == "slide_left" else 1
                c = c + V(sgn * (1 - ease_back(window(t, t0, t0 + 0.5))) * 1200, 0)
            elif anim == "float":
                c = c + V(0, 8 * math.sin(lt * 2.5))
            elif anim == "bounce":
                s *= pop_scale(t, t0, t1)
                c = c - V(0, abs(math.sin(lt * 4.5)) * 24)
            elif anim == "pulse":
                s *= pop_scale(t, t0, t1) * (1.0 + 0.08 * math.sin(lt * 6.0))
            if s > 0.02:
                PROPS[name](cv, c, s, lt, **kw)

        self._raw(draw, t0, t1, z, False)
        return t1

    def caption(self, t0, t1, text, pos="top", size=72, color=INK, typewriter=True, box=False, sfx=None,
                typing_sfx=True):
        """Screen text. pos: 'top' | 'bottom' | 'center' | (x, y) normalized.
        sfx: appear sound (None auto 'swish', False silent). typing_sfx: soft key clicks while typing."""
        xy = {"top": (0.5, 0.1), "bottom": (0.5, 0.92), "center": (0.5, 0.5)}.get(pos, pos)
        self._sfx(t0, self._pick(sfx, "swish"), 0.6)
        if typewriter and typing_sfx and sfx is not False:
            tdur = max(0.3, 0.035 * len(text))
            for i in range(0, len(text), 2):
                if text[i] != " ":
                    self._sfx(t0 + tdur * i / max(1, len(text)), "type", 0.25, pitch=1 + 0.1 * ((i * 7) % 3 - 1))

        def draw(cv, t):
            n = len(text) if not typewriter else int(len(text) * clamp((t - t0) / max(0.3, 0.035 * len(text))))
            tw = cv.text_width(text, size)
            p = V(xy[0] * BW, xy[1] * BH)
            if box:
                cv.rect(p, tw + 60, size * 1.5, 0, 18, SW * 0.8, INK, PAPER)
            cv.text(V(p[0] - tw / 2, p[1]), text[:n], size, color, anchor="lm")

        self._raw(draw, t0, t1, 100, True)
        return t1

    def title(self, t0, t1, text, sub=None, size=130, color=INK, y=0.45, sfx=None):
        """Big centered pop-in title with hand-drawn underline (screen space).
        sfx: None auto (whoosh + impact), False silent, or a SOUNDS name."""
        snd = self._pick(sfx, "whoosh")
        self._sfx(t0, snd, 0.8)
        if sfx is None:
            self._sfx(t0 + 0.3, "impact", 0.5)

        def draw(cv, t):
            s = pop_scale(t, t0, t1, 0.5, 0.25)
            if s < 0.02:
                return
            p = V(BW / 2, y * BH)
            cv.text(p, text, size * s, color)
            tw = cv.text_width(text, size * s)
            u = ease_out(window(t, t0 + 0.3, t0 + 0.8))
            if u > 0:
                cv.curve([p + V(-tw / 2, size * 0.55 * s), p + V(lerp(-tw / 2, tw / 2, u), size * 0.6 * s)],
                         SW * 1.1, RED, wob=1.5)
            if sub:
                cv.text(p + V(0, size * 1.0 * s), sub, size * 0.4 * s, GRAY)

        self._raw(draw, t0, t1, 101, True)
        return t1

    def speech(self, char, t0, t1, text, kind="speech", side="auto", size=44, typewriter=True, talk=True,
               sfx=None, voice=False, voice_pitch=1.0):
        """Speech or thought bubble above a character. kind: 'speech' | 'thought'.
        sfx: bubble appear sound (None auto, False silent). voice=True adds cartoon 'blip' talking sounds
        (voice_pitch: 0.7 deep .. 1.5 squeaky)."""
        ch = self.chars[char]
        lines = textwrap.wrap(text, 18) or [""]
        pan = self._pan(at=char)
        self._sfx(t0, self._pick(sfx, "bubble" if kind == "speech" else "magic"), 0.7, pan)
        if voice and kind == "speech":
            r = random.Random(sd(text))
            tend = min(t1, t0 + 0.06 * len(text) + 0.4)
            tv = t0 + 0.15
            while tv < tend - 0.1:
                self._sfx(tv, "blip", 0.35, pan, voice_pitch * r.uniform(0.8, 1.3))
                tv += r.uniform(0.07, 0.13)
        if talk and kind == "speech":
            ch.layer("talk", t0, min(t1, t0 + 0.06 * len(text) + 0.4))

        def draw(cv, t):
            s = pop_scale(t, t0, t1, 0.35, 0.18)
            if s < 0.03:
                return
            head = ch.anchor(t, "head")
            hh = ch.rig(t)["hh"]
            sd_ = side if side != "auto" else (1 if head[0] < BW * 0.6 else -1)
            wmax = max(cv.text_width(ln, size) for ln in lines) + 60
            hbox = len(lines) * size * 1.25 + 40
            c = head + V(sd_ * (0.35 * hh + wmax / 2), -0.45 * hh - hbox / 2)
            c[0] = clamp(c[0], wmax / 2 + 20, BW - wmax / 2 - 20)
            c[1] = max(c[1], hbox / 2 + 20)
            wb, hb = wmax * s, hbox * s
            tip = head + V(sd_ * 0.18 * hh, -0.2 * hh)
            if kind == "thought":
                cv.ellipse(c, wb / 2 + 15, hb / 2 + 10, 0, SW * 0.8, INK, PAPER)
                for i, r in enumerate((14, 9)):
                    p = lerp(tip, c + V(0, hb / 2), 0.25 + 0.35 * (1 - i))
                    cv.circle(p, r * s, SW * 0.6, INK, PAPER, wob=0)
            else:
                base = c + V(-sd_ * wb * 0.15, hb / 2 - 4)
                cv.polygon([base + V(-16 * s, 0), tip, base + V(16 * s, 0)], SW * 0.8, INK, PAPER)
                cv.rect(c, wb, hb, 0, 28 * s, SW * 0.8, INK, PAPER, wob=0.8)
                cv.line(base + V(-14 * s, -3), base + V(14 * s, -3), SW * 1.2, PAPER, wob=0)
            if s > 0.9:
                total = sum(len(ln) for ln in lines)
                n = total if not typewriter else int(total * clamp((t - t0) / max(0.3, 0.04 * total)))
                y0 = c[1] - (len(lines) - 1) * size * 1.25 / 2
                for i, ln in enumerate(lines):
                    cv.text(V(c[0], y0 + i * size * 1.25), ln[:max(0, n)], size)
                    n -= len(ln)

        self._raw(draw, t0, t1, 60, False)
        return t1

    def counter(self, t0, t1, start, end, x=0.85, y=0.1, prefix="", suffix="", size=80, color=GREEN,
                ease="out", count_dur=None, sfx=None):
        """Animated number (screen space), e.g. money counter.
        sfx: None auto (ticks while counting + end sound), False silent, or a SOUNDS name for the end."""
        cd = count_dur or (t1 - t0) * 0.8
        if sfx is not False:
            pan = self._pan(x)
            self._sfx(t0, "pop", 0.6, pan)
            last, last_t = None, t0
            for j in range(1, int(cd / 0.025)):
                tv = t0 + j * 0.025
                v = int(round(lerp(start, end, EASES[ease](window(tv, t0, t0 + cd)))))
                if v != last and (last is None or tv - last_t >= 0.06):
                    self._sfx(tv, "tick", 0.35, pan, 1.0 + 0.4 * (tv - t0) / cd)
                    last, last_t = v, tv
            self._sfx(t0 + cd, self._pick(sfx, "coin" if "$" in prefix + suffix else "ding"), 0.8, pan)

        def draw(cv, t):
            v = lerp(start, end, EASES[ease](window(t, t0, t0 + cd)))
            s = pop_scale(t, t0, t1, 0.3, 0.2)
            cv.text(V(x * BW, y * BH), f"{prefix}{int(round(v)):,}{suffix}", size * max(s, 0.01), color)

        self._raw(draw, t0, t1, 99, True)
        return t1

    def effect(self, kind, t0, t1, at=None, anchor="head", x=0.5, y=0.5, offset=(0.0, 0.0), z=15,
               count=None, color=None, intensity=1.0, sfx=None, volume=1.0):
        """Particle / screen effects. kind: EFFECT_KINDS.
        sfx: None = automatic sound (EFFECT_SFX), False = silent, or a SOUNDS name."""
        if kind not in EFFECT_KINDS:
            raise ValueError(f"Unknown effect '{kind}'. Available: {EFFECT_KINDS}")
        snd = self._pick(sfx, EFFECT_SFX.get(kind))
        pan = self._pan(None if at else x, at)
        if snd == "rumble":
            self._sfx(t0, snd, volume * min(1.0, 0.5 + 0.5 * intensity), pan, dur=round(max(0.2, t1 - t0), 2))
        else:
            self._sfx(t0, snd, volume, pan)
        if sfx is None and kind == "money_rain":
            self._sfx(t0 + 0.6, "coins", 0.5 * volume, -0.3)
            self._sfx(t0 + 1.1, "coins", 0.4 * volume, 0.3, 1.1)
        r = random.Random(sd(f"{kind}{t0}{self.seed}"))
        if kind == "shake":
            self.shakes.append((t0, t1, intensity))
            return t1
        if kind == "flash":
            self.flashes.append((t0, t1, color or PAPER))
            return t1

        if kind in ("money_rain", "confetti"):
            n = count or (28 if kind == "money_rain" else 90)
            parts = [dict(x=r.uniform(40, BW - 40), y0=r.uniform(-800, -40), v=r.uniform(450, 850),
                          rot=r.uniform(0, 6.28), spin=r.uniform(-4, 4), sway=r.uniform(15, 60),
                          ph=r.uniform(0, 6.28), bill=r.random() < 0.6, col=r.choice(CONFETTI_COLORS))
                     for _ in range(n)]

            def make(front):
                def draw(cv, t):
                    lt = t - t0
                    for i, p in enumerate(parts):
                        if (i % 3 == 0) != front:
                            continue
                        yy = p["y0"] + p["v"] * lt
                        if yy > BH + 120:
                            continue
                        c = V(p["x"] + p["sway"] * math.sin(3 * lt + p["ph"]), yy)
                        if kind == "confetti":
                            cv.rect(c, 16, 9 * abs(math.cos(p["spin"] * lt)) + 2, p["rot"] + p["spin"] * lt,
                                    0, 0, None, p["col"])
                        elif p["bill"]:
                            prop_money_bill(cv, c, 0.9, lt, ang=p["rot"] + p["spin"] * lt * 0.6)
                        else:
                            prop_coin(cv, c, 1.1, lt, flip=p["spin"] * lt + p["ph"])
                return draw
            self._raw(make(False), t0, t1, -1, True)
            self._raw(make(True), t0, t1, z, True)
            return t1

        if kind == "coins_burst":
            parts = [(r.uniform(-500, 500), r.uniform(-900, -500), r.uniform(0, 6)) for _ in range(count or 16)]

            def draw(cv, t):
                lt = t - t0
                c0 = self.resolve(t0, at, anchor, offset, x, y)
                for vx, vy, ph in parts:
                    prop_coin(cv, c0 + V(vx * lt, vy * lt + 0.5 * 2200 * lt * lt), 1.1, lt, flip=ph + lt * 8)
        elif kind == "burst":
            def draw(cv, t):
                u = ease_out(window(t, t0, t1))
                c = self.resolve(t, at, anchor, offset, x, y)
                R = 140 * intensity
                for j in range(12):
                    d = rot(V(1, 0), j * math.pi / 6 + 0.2)
                    a, b = lerp(0.4, 1.3, u) * R, lerp(0.7, 1.4, u) * R
                    if b - a > 2:
                        cv.line(c + d * a, c + d * b, SW * 0.9 * (1 - u) + 1, color or INK, wob=0)
        elif kind == "sparkles":
            parts = [(r.uniform(-1, 1), r.uniform(-1, 0.6), r.uniform(0, 1), r.uniform(14, 28)) for _ in range(count or 7)]

            def draw(cv, t):
                lt = t - t0
                c = self.resolve(t, at, anchor, offset, x, y)
                for px, py, ph, rr in parts:
                    k = max(0.0, math.sin(2 * math.pi * (lt * 1.4 + ph)))
                    if k > 0.05:
                        cv.star(c + V(px * 200 * intensity, py * 200 * intensity), rr * k, 4, 0, SW * 0.6, INK, color or YELLOW)
        elif kind == "hearts":
            def draw(cv, t):
                lt = t - t0
                c = self.resolve(t, at, anchor, offset, x, y)
                for i in range(int(lt / 0.3) + 1):
                    a = lt - i * 0.3
                    if 0 <= a < 1.5:
                        p = c + V(40 * math.sin(a * 4 + i) + (i % 3 - 1) * 40, -a * 160)
                        cv.heart(p, 22 * (1 - a / 1.5) + 4, SW * 0.6, INK, color or PINK)
        elif kind == "likes_burst":
            parts = [(r.uniform(-220, 220), r.uniform(-260, -120), r.uniform(0, 0.6), r.choice(("heart", "star", "up")))
                     for _ in range(count or 12)]

            def draw(cv, t):
                lt = t - t0
                c = self.resolve(t, at, anchor, offset, x, y)
                for vx, vy, dly, knd in parts:
                    a = lt - dly
                    if 0 <= a < 1.4:
                        u = a / 1.4
                        p = c + V(vx * u + 20 * math.sin(a * 5), vy * u)
                        sz = 20 * (1 - u * 0.7) * intensity
                        if knd == "heart":
                            cv.heart(p, sz, SW * 0.55, INK, color or PINK)
                        elif knd == "star":
                            cv.star(p, sz, 5, a * 2, SW * 0.55, INK, YELLOW)
                        else:
                            cv.circle(p, sz, SW * 0.55, INK, LIGHT_BLUE, wob=0)
                            cv.text(p, "+1", sz * 0.9, PAPER)
        elif kind == "fireworks":
            n_b = count or 4
            bursts = [(r.uniform(-320, 320), r.uniform(-220, 120), j * 0.28, r.choice(CONFETTI_COLORS))
                      for j in range(n_b)]

            def draw(cv, t):
                lt = t - t0
                c0 = self.resolve(t, at, anchor, offset, x, y)
                for bx, by, dly, col in bursts:
                    a = lt - dly
                    if 0 <= a < 1.1:
                        u = ease_out(a / 1.1)
                        bc = c0 + V(bx * intensity, by * intensity)
                        R = 160 * intensity * u
                        for k in range(14):
                            d = rot(V(1, 0), k * 2 * math.pi / 14)
                            p1 = bc + d * (R * 0.55) + V(0, 45 * u * u)
                            p2 = bc + d * R + V(0, 45 * u * u)
                            cv.line(p1, p2, SW * 0.75 * (1 - u * 0.8) + 1, color or col, wob=0)
                        if u < 0.7:
                            cv.star(bc, 22 * (1 - u), 4, a * 4, SW * 0.5, INK, YELLOW)
        elif kind == "speed_lines":
            parts = [(r.uniform(0, BH), r.uniform(0, BW), r.uniform(150, 400)) for _ in range(count or 18)]

            def draw(cv, t):
                lt = t - t0
                for yy, x0, ln in parts:
                    xx = (x0 - lt * 2600 * intensity) % (BW + 600) - 300
                    cv.line(V(xx, yy), V(xx + ln, yy), SW * 0.5, color or GRAY, wob=0)
        elif kind == "smoke":
            parts = [(rot(V(1, 0), j * 1.05 + r.uniform(-0.3, 0.3)), r.uniform(20, 36)) for j in range(count or 6)]

            def draw(cv, t):
                u = ease_out(window(t, t0, t1))
                c = self.resolve(t, at, anchor, offset, x, y)
                for d, rr in parts:
                    rad = rr * (0.4 + 0.8 * u) * (1 - 0.6 * u)
                    if rad > 2:
                        cv.circle(c + d * 90 * u * intensity, rad, SW * 0.7 * (1 - u) + 1, INK, PAPER)
        screen = kind == "speed_lines"
        self._raw(draw, t0, t1, z, screen)
        return t1

    def camera(self, t, zoom=1.0, x=0.5, y=0.5, dur=0.6, ease="io", char=None, anchor="chest"):
        """Move/zoom the camera to look at normalized (x, y) or at character `char`."""
        self.cam_keys.append((t, t + dur, dict(z=zoom, x=x, y=y, char=char, anchor=anchor), ease))
        return t + dur

    def fade_in(self, dur=0.4, color=PAPER):
        self.fades.append((0.0, dur, "in", color))

    def fade_out(self, dur=0.5, color=PAPER):
        self.fades.append((self.duration - dur, self.duration, "out", color))

    # ------------------------------------------------------------ rendering
    def _resolve_cam(self):
        keys = []
        for ts, te, d, e in sorted(self.cam_keys, key=lambda k: k[0]):
            if d["char"]:
                p = self.chars[d["char"]].anchor(te, d["anchor"])
                cx, cy = p[0], p[1]
            else:
                cx, cy = d["x"] * BW, d["y"] * BH
            keys.append((ts, te, {"cx": cx, "cy": cy, "z": d["z"]}, e))
        for c in self.chars.values():
            c._cache_t = None
        return keys

    def _cam(self, t, keys):
        c = _fold(keys, {"cx": BW / 2, "cy": BH / 2, "z": 1.0}, t)
        cx, cy = c["cx"], c["cy"]
        for t0, t1, inten in self.shakes:
            if t0 <= t <= t1:
                k = inten * 18 * (1 - window(t, t0, t1) * 0.7)
                cx += k * math.sin(t * 71.0)
                cy += k * math.sin(t * 53.0 + 1.3)
        return (cx, cy, c["z"])

    def _bg(self, cv, t):
        bg = self.background
        if callable(bg):
            bg(cv, t)
            return
        if bg not in ("grid", "dots"):
            return
        cx, cy, z = cv.cam
        x0, x1 = cx - BW / 2 / z, cx + BW / 2 / z
        y0, y1 = cy - BH / 2 / z, cy + BH / 2 / z
        cell = HC / 7
        w = max(1, int(round(1.2 * cv.k)))
        xs = np.arange(math.floor(x0 / cell) * cell, x1 + cell, cell)
        ys = np.arange(math.floor(y0 / cell) * cell, y1 + cell, cell)
        if bg == "grid":
            for gx in xs:
                cv.d.line([cv.tp(gx, y0), cv.tp(gx, y1)], fill=GRID, width=w)
            for gy in ys:
                cv.d.line([cv.tp(x0, gy), cv.tp(x1, gy)], fill=GRID, width=w)
        else:
            for gx in xs:
                for gy in ys:
                    cv.dot(V(gx, gy), 2.2, GRID)

    def render_frame(self, t, width=960, ss=1, cam_keys=None):
        height = int(round(width * BH / BW)) // 2 * 2
        k = width / BW * ss
        cam_keys = self._resolve_cam() if cam_keys is None else cam_keys
        base = self.bg_color if not isinstance(self.background, tuple) else self.background
        img = Image.new("RGB", (width * ss, height * ss), base)
        cv = Canvas(img, k, self._cam(t, cam_keys))
        self._bg(cv, t)
        for z, order, t0, t1, screen, fn in sorted(self.items, key=lambda i: (i[0], i[1])):
            if t0 <= t <= t1:
                cv.screen = screen
                fn(cv, t)
        if ss > 1:
            img = img.resize((width, height), Image.LANCZOS)
        for t0, t1, mode, col in self.fades:
            if t0 <= t <= t1:
                a = 1 - window(t, t0, t1) if mode == "in" else window(t, t0, t1)
                img = Image.blend(img, Image.new("RGB", img.size, col), clamp(a))
        for t0, t1, col in self.flashes:
            if t0 <= t <= t1:
                img = Image.blend(img, Image.new("RGB", img.size, col), clamp(1 - window(t, t0, t1)) * 0.85)
        return img

    def render(self, out="animation.mp4"):
        """Render the video. CLI flags: --out FILE --preview --frame N --ss 2 --fps 30 --width 1920
        --no-sound (silent video) --audio-only (write only the WAV) --keep-wav."""
        ap = argparse.ArgumentParser()
        ap.add_argument("--out", default=out)
        ap.add_argument("--preview", action="store_true", help="960x540, no supersampling")
        ap.add_argument("--frame", type=int, default=None, help="save one frame as PNG")
        ap.add_argument("--ss", type=int, default=2, help="supersampling (2 smooth, 3 high)")
        ap.add_argument("--fps", type=int, default=self.fps)
        ap.add_argument("--width", type=int, default=self.width)
        ap.add_argument("--no-sound", action="store_true", help="render without audio")
        ap.add_argument("--audio-only", action="store_true", help="only write the soundtrack WAV")
        ap.add_argument("--keep-wav", action="store_true", help="keep the soundtrack WAV next to the video")
        a, _ = ap.parse_known_args()
        width = 960 if a.preview else a.width
        ss = 1 if a.preview else max(1, a.ss)
        cam_keys = self._resolve_cam()
        if a.frame is not None:
            name = f"frame_{a.frame:04d}.png"
            self.render_frame(a.frame / a.fps, width, ss, cam_keys).save(name)
            print("saved", name)
            return
        wav = os.path.splitext(a.out)[0] + ".wav"
        use_audio = self.has_audio() and not a.no_sound
        if a.audio_only:
            write_wav(wav, self.render_audio())
            print("saved", wav)
            return
        import imageio.v2 as imageio
        vid = os.path.splitext(a.out)[0] + ".silent.mp4" if use_audio else a.out
        n = int(round(self.duration * a.fps))
        wr = imageio.get_writer(vid, fps=a.fps, codec="libx264", quality=8,
                                pixelformat="yuv420p", macro_block_size=2)
        for i in range(n):
            wr.append_data(np.asarray(self.render_frame(i / a.fps, width, ss, cam_keys)))
            if i % 15 == 0 or i == n - 1:
                print(f"\rframe {i + 1}/{n}", end="", flush=True)
        wr.close()
        print()
        if use_audio:
            print(f"mixing {len(self.sfx)} sound events" + (" + music" if self.music_tracks else ""))
            write_wav(wav, self.render_audio())
            try:
                mux_audio(vid, wav, a.out)
                os.remove(vid)
                if not a.keep_wav:
                    os.remove(wav)
            except Exception as e:                       # keep a usable result even if muxing fails
                print("audio mux failed:", e)
                os.replace(vid, a.out)
                print("silent video saved; soundtrack saved to", wav)
        print(f"saved {a.out}")


# =============================================================== demo
if __name__ == "__main__":
    scene = Scene(duration=6)
    bob = scene.add_character("bob", x=0.3)
    scene.fade_in(0.3)
    scene.music("upbeat", volume=0.25)              # sound effects below are added automatically
    bob.pose(0.2, "wave", dur=0.4, ease="back", face="happy")
    bob.layer("wave", 0.4, 1.6)
    scene.speech("bob", 0.4, 1.8, "Hi! Want to make money?")
    bob.pose(1.8, "think", dur=0.4, face="thinking")
    t = bob.pose(2.6, "point_up", dur=0.3, ease="back", face="excited")
    scene.prop("lightbulb", 2.6, 3.3, at="bob", anchor="above_head")
    scene.effect("burst", 2.6, 3.0, at="bob", anchor="above_head")
    t = bob.walk_to(3.3, 0.55)
    bob.pose(t, "cheer", dur=0.3, ease="back", face="very_happy")
    bob.jump(t + 0.2)
    scene.effect("money_rain", t, 6)
    scene.counter(t, 6, 0, 10000, prefix="$")
    scene.camera(3.3, zoom=1.15, char="bob", dur=1.0)
    scene.fade_out(0.4)
    scene.render("demo.mp4")
