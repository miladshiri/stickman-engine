#!/usr/bin/env python3
"""
stickman_json.py - render a stickman video from a JSON scene file (no Python scene code needed).

    python3 stickman_json.py scenes/habits.json --check      # validate only, lists every problem
    python3 stickman_json.py scenes/habits.json --preview    # fast 960x540 render
    python3 stickman_json.py scenes/habits.json              # final 1920x1080 render

All render flags of stickman_engine work: --preview --frame N --out FILE --ss 3 --fps --width
--no-sound --audio-only --keep-wav.  The JSON format is documented in MASTER_PROMPT_JSON.md.
"""
import difflib
import inspect
import json
import math
import os
import sys

import stickman_engine as E
from stickman_engine import (BH, BW, SW, EASES, EFFECT_KINDS, FACES, LAYER_KINDS, MUSIC_STYLES, POSES,
                             PROPS, SOUNDS, Scene, V, clamp, ease_in, ease_io, ease_out, lerp, pop_scale,
                             window)

# =============================================================== vocabularies
COLOR_NAMES = ["INK", "PAPER", "GRID", "GREEN", "LIGHT_GREEN", "GOLD", "YELLOW", "RED", "PINK", "BLUE",
               "LIGHT_BLUE", "GRAY", "LIGHT_GRAY", "ORANGE", "PURPLE"]
ANCHORS = ["head", "above_head", "top", "face", "mouth", "chest", "hip", "feet", "hand_r", "hand_l",
           "left", "right"]
ANIMS = ["pop", "drop", "grow", "slide_left", "slide_right", "float", "bounce", "pulse", "none"]
ONLY = ["arms", "right", "left", "body", "legs"]
HANDS = list(E.HAND_SHAPES)
POSE_FIELDS = ["r", "l", "crouch", "tilt", "lean", "hipx", "stance", "liftl", "liftr", "sit", "kneel",
               "lh", "rh"]
BACKGROUNDS = ["grid", "white", "dots"]
CAPTION_POS = ["top", "bottom", "center"]

# Extra keyword arguments each prop accepts (read from the prop functions' signatures).
PROP_ARGS = {name: [p for p in list(inspect.signature(fn).parameters)[4:]] for name, fn in PROPS.items()}

# Keys allowed on every event. "note" / "comment" are ignored, so the LLM can annotate beats.
COMMON = {"type", "start", "note", "comment"}

# type -> (required keys, optional keys)
# "end" in required means the event spans start..end.
SPEC = {
    # --- character actions (need "char")
    "pose":       ({"char", "pose"}, {"dur", "ease", "face", "only", *POSE_FIELDS}),
    "expression": ({"char", "face"}, {"dur", "ease"}),
    "look":       ({"char"}, {"x", "y", "dur"}),
    "layer":      ({"char", "layer", "end"}, {"amount"}),
    "walk":       ({"char", "x"}, {"dur", "ease", "sfx", "face_front"}),
    "run":        ({"char", "x"}, {"dur", "ease", "sfx", "face_front"}),
    "move":       ({"char"}, {"x", "y", "dur", "ease"}),
    "jump":       ({"char"}, {"height", "dur", "sfx"}),
    "turn":       ({"char", "facing"}, {"dur"}),
    "show":       ({"char"}, {"dur", "sfx"}),
    "hide":       ({"char"}, {"dur", "sfx"}),
    "hold":       ({"char", "prop", "end"}, {"hand", "scale"}),          # + prop args
    # --- scene elements
    "caption":    ({"text", "end"}, {"pos", "size", "color", "typewriter", "box", "sfx", "typing_sfx"}),
    "title":      ({"text", "end"}, {"sub", "size", "color", "y", "sfx"}),
    "speech":     ({"char", "text", "end"}, {"kind", "side", "size", "typewriter", "voice", "voice_pitch",
                                             "sfx"}),
    "counter":    ({"from", "to", "end"}, {"x", "y", "prefix", "suffix", "size", "color", "ease",
                                           "count_dur", "sfx"}),
    "prop":       ({"prop", "end"}, {"x", "y", "char", "anchor", "offset", "scale", "anim", "z", "sfx",
                                     "volume"}),                          # + prop args
    "effect":     ({"effect", "end"}, {"x", "y", "char", "anchor", "offset", "count", "color", "intensity",
                                       "z", "sfx", "volume"}),
    "camera":     (set(), {"zoom", "x", "y", "dur", "ease", "char", "anchor"}),
    "sound":      ({"sound"}, {"volume", "pan", "pitch", "dur"}),
    # --- simple motion graphics (drawn by this file)
    "arrow":      ({"from", "to", "end"}, {"color", "width", "bend", "draw", "z", "sfx"}),
    "circle":     ({"x", "y", "r", "end"}, {"color", "fill", "width", "z", "sfx", "text", "text_size",
                                            "text_color"}),
    "rect":       ({"x", "y", "w", "h", "end"}, {"color", "fill", "width", "radius", "z", "sfx", "text",
                                                 "text_size", "text_color"}),
    "progress":   ({"x", "y", "end"}, {"w", "h", "from", "to", "fill_dur", "color", "label", "show_value",
                                       "z", "sfx"}),
}
TOP_KEYS = {"duration", "fps", "background", "output", "sound", "sfx_volume", "fade_in", "fade_out",
            "music", "characters", "events", "title", "description", "note", "comment"}
CHAR_KEYS = {"name", "x", "ground", "scale", "color", "facing", "z", "visible"}
MUSIC_KEYS = {"style", "volume", "start", "end", "bpm", "file"}


# =============================================================== helpers
def color(v):
    """'green' / 'GREEN' / '#28a050' / [r, g, b] -> (r, g, b)."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return tuple(int(c) for c in v)
    if isinstance(v, str):
        if v.startswith("#") and len(v) == 7:
            return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
        if v.upper() in COLOR_NAMES:
            return getattr(E, v.upper())
    raise ValueError(f"bad color {v!r}")


def is_color(v):
    try:
        color(v)
        return True
    except (ValueError, TypeError):
        return False


def _suggest(word, options):
    m = difflib.get_close_matches(str(word), [str(o) for o in options], n=3, cutoff=0.5)
    return f" Did you mean {', '.join(repr(x) for x in m)}?" if m else ""


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pt(p):
    return V(p[0] * BW, p[1] * BH)


# =============================================================== validation
def validate(cfg):
    """Return a list of human/LLM-readable error strings (empty list = valid)."""
    errs = []
    if not isinstance(cfg, dict):
        return ["The top level of the file must be a JSON object { ... }."]

    for k in cfg:
        if k not in TOP_KEYS:
            errs.append(f"top level: unknown key '{k}'.{_suggest(k, TOP_KEYS)}")
    dur = cfg.get("duration")
    if not _num(dur) or dur <= 0:
        errs.append("top level: 'duration' (seconds, > 0) is required.")
        dur = float("inf")
    bg = cfg.get("background", "grid")
    if not (bg in BACKGROUNDS or is_color(bg)):
        errs.append(f"top level: background {bg!r} must be one of {BACKGROUNDS} or a color.")
    for k in ("fade_in", "fade_out"):
        if k in cfg and cfg[k] is not None and not _num(cfg[k]):
            errs.append(f"top level: '{k}' must be a number of seconds (or null).")

    m = cfg.get("music")
    if m:
        if not isinstance(m, dict):
            errs.append("music: must be an object like {\"style\": \"upbeat\", \"volume\": 0.25} or null.")
        else:
            for k in m:
                if k not in MUSIC_KEYS:
                    errs.append(f"music: unknown key '{k}'.{_suggest(k, MUSIC_KEYS)}")
            if "file" not in m and m.get("style", "upbeat") not in MUSIC_STYLES:
                errs.append(f"music: style {m.get('style')!r} unknown. Use one of {sorted(MUSIC_STYLES)}."
                            + _suggest(m.get("style"), MUSIC_STYLES))

    chars = {}
    for i, c in enumerate(cfg.get("characters", []) or []):
        where = f"characters[{i}]"
        if not isinstance(c, dict) or "name" not in c:
            errs.append(f"{where}: must be an object with a 'name'.")
            continue
        for k in c:
            if k not in CHAR_KEYS:
                errs.append(f"{where} ({c['name']}): unknown key '{k}'.{_suggest(k, CHAR_KEYS)}")
        if "color" in c and not is_color(c["color"]):
            errs.append(f"{where}: bad color {c['color']!r}.")
        if c["name"] in chars:
            errs.append(f"{where}: duplicate character name '{c['name']}'.")
        chars[c["name"]] = c

    events = cfg.get("events")
    if not isinstance(events, list):
        errs.append("top level: 'events' must be a list.")
        return errs
    for i, e in enumerate(events):
        errs += _validate_event(i, e, dur, chars)
    return errs


def _validate_event(i, e, dur, chars):
    errs = []
    if not isinstance(e, dict):
        return [f"events[{i}]: must be an object."]
    typ = e.get("type")
    where = f"events[{i}] ({typ} @ {e.get('start', '?')}s)"
    if typ not in SPEC:
        return [f"events[{i}]: unknown type {typ!r}.{_suggest(typ, SPEC)} Types: {sorted(SPEC)}"]
    req, opt = SPEC[typ]
    allowed = set(req) | set(opt) | COMMON
    prop_name = e.get("prop")
    if typ in ("prop", "hold") and prop_name in PROPS:
        allowed |= set(PROP_ARGS[prop_name])

    def err(msg):
        errs.append(f"{where}: {msg}")

    for k in sorted(set(req) | {"start"}):
        if k not in e:
            err(f"missing required key '{k}'.")
    for k in e:
        if k not in allowed:
            hint = ""
            if typ in ("prop", "hold") and prop_name in PROPS:
                hint = f" Prop '{prop_name}' accepts: {PROP_ARGS[prop_name] or 'no extra args'}."
            err(f"unknown key '{k}'.{_suggest(k, allowed)}{hint}")

    # ---- timing
    t0, t1 = e.get("start"), e.get("end")
    if "start" in e and (not _num(t0) or t0 < 0 or t0 > dur):
        err(f"'start' must be a number between 0 and duration ({dur}).")
    if "end" in e:
        if not _num(t1):
            err("'end' must be a number.")
        elif _num(t0) and t1 <= t0:
            err(f"'end' ({t1}) must be after 'start' ({t0}).")
        elif t1 > dur + 1e-6:
            err(f"'end' ({t1}) is after the video duration ({dur}).")

    # ---- names
    def check(key, options, label=None):
        if key in e and e[key] not in options:
            err(f"unknown {label or key} {e[key]!r}.{_suggest(e[key], options)}")

    if "char" in e and e["char"] not in chars:
        err(f"character {e['char']!r} is not defined in 'characters'.{_suggest(e['char'], chars)}")
    check("pose", POSES)
    check("face", FACES)
    check("layer", LAYER_KINDS)
    check("effect", EFFECT_KINDS)
    check("sound", SOUNDS)
    check("ease", EASES)
    check("anchor", ANCHORS)
    check("anim", ANIMS)
    check("only", ONLY)
    check("lh", HANDS, "hand shape")
    check("rh", HANDS, "hand shape")
    check("hand", ["r", "l"])
    if typ in ("prop", "hold"):
        check("prop", PROPS)
    if typ == "caption" and isinstance(e.get("pos"), str):
        check("pos", CAPTION_POS)
    if typ == "speech":
        check("kind", ["speech", "thought"])
    if "sfx" in e and isinstance(e["sfx"], str) and e["sfx"] not in SOUNDS and e["sfx"] != "none":
        err(f"unknown sfx {e['sfx']!r}.{_suggest(e['sfx'], SOUNDS)} Use a sound name, true/null (auto) or false.")
    for k in ("color", "fill", "text_color"):
        if k in e and e[k] is not None and not is_color(e[k]):
            err(f"bad {k} {e[k]!r}. Use a name like 'GREEN', '#28a050' or [r, g, b].")
    for k in ("r", "l"):
        if typ == "pose" and k in e and not (isinstance(e[k], list) and len(e[k]) == 2 and all(map(_num, e[k]))):
            err(f"'{k}' must be [angle, reach].")
    for k in ("from", "to"):
        if typ == "arrow" and k in e and not (isinstance(e[k], list) and len(e[k]) == 2):
            err(f"'{k}' must be a point [x, y] (normalized 0..1).")
    if "offset" in e and not (isinstance(e["offset"], list) and len(e["offset"]) == 2):
        err("'offset' must be [dx, dy] (in character heights).")
    if typ == "caption" and isinstance(e.get("pos"), list) and len(e["pos"]) != 2:
        err("'pos' must be 'top' | 'bottom' | 'center' or [x, y].")
    if typ in ("prop", "effect") and "char" not in e and ("x" not in e or "y" not in e):
        err("give either 'char' (+ optional 'anchor') or both 'x' and 'y'.")
    return errs


# =============================================================== building
def _sfx(e, auto):
    """JSON sfx -> engine sfx. missing/null/true -> auto, false -> silent, 'name' -> that sound."""
    v = e.get("sfx")
    if v is None or v is True:
        return auto
    return v


def _pick(e, keys, rename=None):
    rename = rename or {}
    out = {}
    for k in keys:
        if k in e:
            v = e[k]
            if k in ("color", "fill", "text_color"):
                v = color(v)
            elif k in ("offset", "r", "l", "pos") and isinstance(v, list):
                v = tuple(v)
            out[rename.get(k, k)] = v
    return out


def _prop_kw(e):
    kw = {}
    for k in PROP_ARGS.get(e["prop"], []):
        if k in e:
            v = e[k]
            if k == "color":
                v = color(v)
            elif k in ("bars", "slices", "points") and isinstance(v, list):
                v = tuple(v)
            kw[k] = v
    return kw


def build(cfg):
    """Create a stickman_engine Scene from a validated config dict."""
    bg = cfg.get("background", "grid")
    scene = Scene(duration=cfg["duration"], fps=cfg.get("fps", 30),
                  background=bg if bg in BACKGROUNDS else color(bg),
                  sound=cfg.get("sound", True), sfx_volume=cfg.get("sfx_volume", 1.0))

    for c in cfg.get("characters", []) or []:
        kw = _pick(c, ["x", "ground", "scale", "color", "facing", "z", "visible"])
        scene.add_character(c["name"], **kw)

    if cfg.get("fade_in"):
        scene.fade_in(cfg["fade_in"])
    m = cfg.get("music")
    if m:
        scene.music(style=m.get("style", "upbeat"), volume=m.get("volume", 0.25), t0=m.get("start", 0.0),
                    t1=m.get("end"), bpm=m.get("bpm"), path=m.get("file"))

    events = sorted(enumerate(cfg["events"]), key=lambda ie: (ie[1]["start"], ie[0]))
    for _, e in events:
        BUILDERS[e["type"]](scene, e)

    if cfg.get("fade_out"):
        scene.fade_out(cfg["fade_out"])
    return scene


def _char(scene, e):
    return scene.chars[e["char"]]


def b_pose(scene, e):
    kw = _pick(e, ["dur", "ease", "face", "only", *POSE_FIELDS])
    _char(scene, e).pose(e["start"], e["pose"], **kw)


def b_expression(scene, e):
    _char(scene, e).expression(e["start"], e["face"], **_pick(e, ["dur", "ease"]))


def b_look(scene, e):
    _char(scene, e).look(e["start"], **_pick(e, ["x", "y", "dur"]))


def b_layer(scene, e):
    _char(scene, e).layer(e["layer"], e["start"], e["end"], **_pick(e, ["amount"]))


def b_walk(scene, e, run=False):
    kw = _pick(e, ["dur", "ease", "face_front"])
    if "sfx" in e:
        kw["sfx"] = bool(e["sfx"])
    _char(scene, e).walk_to(e["start"], e["x"], run=run or e["type"] == "run", **kw)


def b_move(scene, e):
    _char(scene, e).move_to(e["start"], **_pick(e, ["x", "y", "dur", "ease"]))


def b_jump(scene, e):
    kw = _pick(e, ["height", "dur"])
    if "sfx" in e:
        kw["sfx"] = e["sfx"] if isinstance(e["sfx"], str) else bool(e["sfx"])
    _char(scene, e).jump(e["start"], **kw)


def b_turn(scene, e):
    _char(scene, e).turn(e["start"], e["facing"], **_pick(e, ["dur"]))


def b_show(scene, e):
    kw = _pick(e, ["dur"])
    if "sfx" in e:
        kw["sfx"] = _sfx(e, "pop") or None
    _char(scene, e).show(e["start"], **kw)


def b_hide(scene, e):
    kw = _pick(e, ["dur"])
    if "sfx" in e:
        kw["sfx"] = _sfx(e, "swish") or None
    _char(scene, e).hide(e["start"], **kw)


def b_hold(scene, e):
    _char(scene, e).hold(e["prop"], e["start"], e["end"], **_pick(e, ["hand", "scale"]), **_prop_kw(e))


def b_caption(scene, e):
    kw = _pick(e, ["pos", "size", "color", "typewriter", "box", "typing_sfx"])
    scene.caption(e["start"], e["end"], e["text"], sfx=_sfx(e, None), **kw)


def b_title(scene, e):
    scene.title(e["start"], e["end"], e["text"], sfx=_sfx(e, None), **_pick(e, ["sub", "size", "color", "y"]))


def b_speech(scene, e):
    kw = _pick(e, ["kind", "side", "size", "typewriter", "voice", "voice_pitch"])
    scene.speech(e["char"], e["start"], e["end"], e["text"], sfx=_sfx(e, None), **kw)


def b_counter(scene, e):
    kw = _pick(e, ["x", "y", "prefix", "suffix", "size", "color", "ease", "count_dur"])
    scene.counter(e["start"], e["end"], e["from"], e["to"], sfx=_sfx(e, None), **kw)


def b_prop(scene, e):
    kw = _pick(e, ["x", "y", "anchor", "offset", "scale", "anim", "z", "volume"])
    if "char" in e:
        kw["at"] = e["char"]
    scene.prop(e["prop"], e["start"], e["end"], sfx=_sfx(e, None), **kw, **_prop_kw(e))


def b_effect(scene, e):
    kw = _pick(e, ["x", "y", "anchor", "offset", "count", "color", "intensity", "z", "volume"])
    if "char" in e:
        kw["at"] = e["char"]
    scene.effect(e["effect"], e["start"], e["end"], sfx=_sfx(e, None), **kw)


def b_camera(scene, e):
    scene.camera(e["start"], **_pick(e, ["x", "y", "dur", "ease", "char", "anchor"]),
                 **({"zoom": e["zoom"]} if "zoom" in e else {}))


def b_sound(scene, e):
    scene.sound(e["start"], e["sound"], **_pick(e, ["volume", "pan", "pitch", "dur"]))


# ---------------------------------------------------------------- motion graphics
def _graphic_sfx(scene, e, auto, x=0.5):
    s = _sfx(e, auto)
    if s and s != "none":
        scene.sound(e["start"], s, volume=0.6, pan=clamp((x - 0.5) * 1.2, -0.8, 0.8))


def _label(cv, c, e, s, default_size):
    if e.get("text"):
        cv.text(c, e["text"], size=e.get("text_size", default_size) * s,
                color=color(e.get("text_color", "INK")))


def b_arrow(scene, e):
    """Arrow that draws itself on from 'from' to 'to', then retracts at the end."""
    p0, p1 = _pt(e["from"]), _pt(e["to"])
    t0, t1 = e["start"], e["end"]
    draw = min(e.get("draw", 0.5), (t1 - t0) / 2)
    col, w, bend = color(e.get("color", "INK")), e.get("width", SW), e.get("bend", 0.0)

    def fn(g):
        u = ease_out(window(g.t, t0, t0 + draw))
        v = ease_in(window(g.t, t1 - 0.25, t1))        # retract from the tail
        if u - v < 0.03:
            return
        a, b = lerp(p0, p1, v), lerp(p0, p1, u)
        g.cv.arrow(a, b, w=w, color=col, bend=bend)

    scene.add(fn, t0, t1, z=e.get("z", 20))
    _graphic_sfx(scene, e, "whoosh", e["from"][0])


def b_circle(scene, e):
    """Circle (highlight ring or filled badge) that pops in/out. r is a fraction of screen height."""
    c, r = _pt((e["x"], e["y"])), e["r"] * BH
    t0, t1 = e["start"], e["end"]
    col, fill, w = color(e.get("color", "INK")), color(e.get("fill")), e.get("width", SW)

    def fn(g):
        s = pop_scale(g.t, t0, t1)
        if s > 0.02:
            g.cv.circle(c, r * s, w=w * max(s, 0.3), color=col, fill=fill)
            _label(g.cv, c, e, s, 48)

    scene.add(fn, t0, t1, z=e.get("z", 20))
    _graphic_sfx(scene, e, "pop", e["x"])


def b_rect(scene, e):
    """Rounded box / card / label that pops in/out. w, h are fractions of screen width / height."""
    c, wd, ht = _pt((e["x"], e["y"])), e["w"] * BW, e["h"] * BH
    t0, t1 = e["start"], e["end"]
    col, fill, w = color(e.get("color", "INK")), color(e.get("fill")), e.get("width", SW)
    radius = e.get("radius", 18)

    def fn(g):
        s = pop_scale(g.t, t0, t1)
        if s > 0.02:
            g.cv.rect(c, wd * s, ht * s, radius=radius * s, w=w * max(s, 0.3), color=col, fill=fill)
            _label(g.cv, c, e, s, 48)

    scene.add(fn, t0, t1, z=e.get("z", 20))
    _graphic_sfx(scene, e, "pop", e["x"])


def b_progress(scene, e):
    """Progress bar that fills from 'from' to 'to' (0..1). Optional label above, % value inside."""
    c = _pt((e["x"], e["y"]))
    wd, ht = e.get("w", 0.3) * BW, e.get("h", 0.05) * BH
    t0, t1 = e["start"], e["end"]
    a, b = e.get("from", 0.0), e.get("to", 1.0)
    fill_dur = e.get("fill_dur", 0.6 * (t1 - t0))
    col = color(e.get("color", "GREEN"))

    def fn(g):
        s = pop_scale(g.t, t0, t1, din=0.3)
        if s <= 0.02:
            return
        cv = g.cv
        v = lerp(a, b, ease_io(window(g.t, t0 + 0.3, t0 + 0.3 + fill_dur)))
        W, H = wd * s, ht * s
        if v > 0.005:
            fw = W * clamp(v)
            cv.rect(c + V(-(W - fw) / 2, 0), fw, H, radius=H * 0.3, w=0.1, color=col, fill=col)
        cv.rect(c, W, H, radius=H * 0.3, w=SW * max(s, 0.3), color=E.INK, fill=None)
        if e.get("label"):
            cv.text(c + V(0, -H * 0.5 - 34 * s), e["label"], size=44 * s, color=E.INK)
        if e.get("show_value", True):
            cv.text(c, f"{round(v * 100)}%", size=max(20, H * 0.6), color=E.INK)

    scene.add(fn, t0, t1, z=e.get("z", 20))
    _graphic_sfx(scene, e, "rise", e["x"])


BUILDERS = {
    "pose": b_pose, "expression": b_expression, "look": b_look, "layer": b_layer, "walk": b_walk,
    "run": b_walk, "move": b_move, "jump": b_jump, "turn": b_turn, "show": b_show, "hide": b_hide,
    "hold": b_hold, "caption": b_caption, "title": b_title, "speech": b_speech, "counter": b_counter,
    "prop": b_prop, "effect": b_effect, "camera": b_camera, "sound": b_sound, "arrow": b_arrow,
    "circle": b_circle, "rect": b_rect, "progress": b_progress,
}
assert set(BUILDERS) == set(SPEC)


# =============================================================== CLI
def load(path):
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as ex:
            sys.exit(f"{path}: invalid JSON at line {ex.lineno}, column {ex.colno}: {ex.msg}")


def main():
    files = [a for a in sys.argv[1:] if a.endswith(".json")]
    if len(files) != 1:
        sys.exit("usage: python3 stickman_json.py scene.json [--check] [--preview] [--frame N] [--out f.mp4] ...")
    path = files[0]
    cfg = load(path)
    errs = validate(cfg)
    if errs:
        print(f"{path}: {len(errs)} problem(s) found:")
        for x in errs:
            print("  -", x)
        sys.exit(1)
    if "--check" in sys.argv:
        print(f"{path}: OK ({len(cfg['events'])} events, {cfg['duration']} s)")
        return
    scene = build(cfg)
    out = cfg.get("output") or os.path.splitext(os.path.basename(path))[0] + ".mp4"
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != path]
    scene.render(out)


if __name__ == "__main__":
    main()

