---
name: demo
description: Run the web app on demo documents with a stand-in vision model (no API key needed), and remake the docs' animated tours (docs/tours/*.webp) or the website's hero video (hero-video/). Use to see how a UI change looks with realistic data, or to refresh the docs' media after changing a page.
argument-hint: "[serve | tours [names...] | hero]"
---

# Demo

Everything here runs offline on data it makes itself, so the docs' media never depend on a real file or an API key.

- `demo_data.py`: three sample documents (a born-digital report, a scanned page of field notes, a scanned invoice), a
  stand-in OpenAI-compatible vision model on localhost (model `demo-vision`), and the real app started on an empty
  data folder and seeded through its own API. `--help` describes the data.
- `tour_shots.py`: plays a scripted visit to each view in headless Chromium (a drawn pointer, click rings, typing a
  letter at a time) and writes `docs/tours/<name>-{light,dark}.webp` plus a still of the last frame
  (`<name>-{light,dark}.png`). README.md shows the light WebPs; docs/index.html shows them in the page's theme.
- `../../../hero-video/`: the website's hero video, made with Remotion. Its montage uses the tours' dark stills, so
  make the tours first.

Arguments: `$ARGUMENTS`.

```bash
python .claude/skills/demo/demo_data.py --serve                  # seeded app on http://127.0.0.1:8765
python .claude/skills/demo/tour_shots.py --list
python .claude/skills/demo/tour_shots.py compare --theme dark    # one tour, one theme
python .claude/skills/demo/tour_shots.py                         # every tour, both themes (about 10 minutes)
cd hero-video && npm install && npm run render                   # docs/media/hero-demo.mp4 and hero-poster.webp
```

Needs Tesseract, Pillow and Python Playwright (`pip install playwright`, plus `playwright install chromium` unless the
session already has a Chromium). In a cloud session, install the Python package at the version of the
preinstalled Node Playwright, so it finds the same browser, and render the hero video with that browser and without
a font download: `npx remotion render ... --browser-executable=$(ls -d /opt/pw-browsers/chromium_headless_shell-*/*/chrome-headless-shell)`.

When a page's markup changes, the tours' selectors may need updating: each `play_*` function in `tour_shots.py` is a
short script of the visit. Look at a tour before committing it (the frames that are held longest are the ones a
reader sees). The hero video's numbers (error rates, diffs) are copied from what the app shows on the demo data; if
the demo data changes, check them in the Compare view and update `hero-video/src/scenes/`.

Whenever an LLM result shows in the docs, say that the model is a stand-in.
