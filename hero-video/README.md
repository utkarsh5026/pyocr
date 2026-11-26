# Hero video

The video at the top of the pyocr-toolkit website (`docs/index.html`), made with [Remotion](https://www.remotion.dev/).
The scenes are in `src/scenes/`. Their text and numbers come from the demo data in `.claude/skills/demo/demo_data.py`,
and the montage uses the last frames of the docs' tours, so make the tours first
(`python .claude/skills/demo/tour_shots.py`).

```bash
npm install
npm run dev      # preview at http://localhost:3000
npm run render   # writes ../docs/media/hero-demo.mp4 and hero-poster.webp
```

`npx remotion skills add` installs Remotion's skills for coding agents; they stay out of git.
