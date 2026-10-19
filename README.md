# Stickman Engine

A Python engine for hand-drawn stickman explainer videos (1920×1080 MP4 with synthesized sound effects and music).

You don't have to learn the API yourself. **You describe the video to an LLM, it writes the scene script, and you run it.**

| File | What it is |
|---|---|
| `stickman_engine.py` | The engine. Don't edit it, scenes import it. |
| `MASTER_PROMPT.md` | Instructions + full API reference for an LLM. Paste it into any chat model. |
| `scene_<topic>.py` | A scene script written by the LLM (one per video). |

---

## 1. Setup (once)

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install numpy pillow imageio imageio-ffmpeg
```

`imageio-ffmpeg` ships its own ffmpeg, so nothing else needs installing.

Check that it works by rendering the built-in demo:

```bash
python3 stickman_engine.py --preview
```

---

## 2. Get a scene from an LLM

Works with ChatGPT, Claude, Gemini, etc. Bigger / "thinking" models produce better scenes.

1. **Open a new chat** (a clean context works best).
2. **Paste the whole contents of `MASTER_PROMPT.md`** as your first message, or attach the file.
   - macOS shortcut: `pbcopy < MASTER_PROMPT.md` copies it to the clipboard.
3. **The LLM asks you two questions**: what the video should show, and how long it should be. Answer them, for example:
   > A 30-second video on "3 habits that make you rich". One character, Bob, upbeat mood, grid background, upbeat music.

   Optionally mention background (`grid` / `white` / `dots` / a color), number of characters, and music mood.
4. **The LLM replies with:**
   - a beat table (time → what happens),
   - one complete Python file, e.g. `scene_habits.py`,
   - the run commands.

> [!TIP]
> If the model skips the questions and writes code straight away, just reply with your answers and ask it to rewrite the scene.

---

## 3. Save the code and render

1. Create a new file **in the same folder as `stickman_engine.py`**, named the way the LLM suggested (e.g. `scene_habits.py`).
2. Paste the code in and save. It should start with `from stickman_engine import *` and end with `scene.render("habits.mp4")`.
   - macOS shortcut: copy the code block, then run `pbpaste > scene_habits.py`.
3. Render:

```bash
python3 scene_habits.py --preview     # fast 960×540 check, with sound
python3 scene_habits.py               # final 1920×1080 → habits.mp4
```

### Render options

| Flag | Effect |
|---|---|
| `--preview` | 960×540, no supersampling. Much faster, use it while iterating. |
| `--frame 60` | Save only frame 60 as `frame_0060.png` (frame = seconds × fps). |
| `--out name.mp4` | Change the output file name. |
| `--ss 3` | Supersampling quality (default 2; 3 = smoother but slower). |
| `--fps 60` / `--width 1280` | Override frame rate / output width. |
| `--no-sound` | Silent video. |
| `--audio-only` | Write only the soundtrack WAV. |
| `--keep-wav` | Keep the WAV next to the MP4. |

---

## 4. Iterate with the LLM

Keep the same chat open and ask for changes in plain language:

- "Make Bob jump when the chart appears."
- "Add a second character, Alice, who disagrees at 10 s."
- "The caption at 12 s overlaps the chart. Move it to the top."
- "Make it 45 seconds and add a recap at the end."

Paste the new code over your file and re-run with `--preview`.

**If the script crashes**, paste the full error (traceback) back into the chat. Most errors are an unknown pose/prop/effect name or a wrong argument, and the LLM fixes them quickly. Use `--frame N` to check one moment without rendering the whole video.

---

## 5. Writing scenes by hand (optional)

`MASTER_PROMPT.md` doubles as the API reference. A minimal scene:

```python
from stickman_engine import *

scene = Scene(duration=6, background="grid")
bob = scene.add_character("bob", x=0.35)
scene.music("upbeat", volume=0.25)
scene.fade_in(0.3)

bob.pose(0.2, "wave", dur=0.4, ease="back", face="happy")
bob.layer("wave", 0.4, 1.6)
scene.speech("bob", 0.4, 2.0, "Want to grow your savings?", voice=True)
bob.pose(2.2, "point_up", ease="back", face="excited")
scene.prop("lightbulb", 2.2, 4.0, at="bob", anchor="above_head")
t = bob.jump(4.2)
bob.pose(t, "cheer", face="very_happy")
scene.effect("confetti", 4.4, 6)

scene.fade_out(0.4)
scene.render("savings.mp4")
```
