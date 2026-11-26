import { loadFont } from "@remotion/fonts";
import inter400 from "@fontsource/inter/files/inter-latin-400-normal.woff2";
import inter500 from "@fontsource/inter/files/inter-latin-500-normal.woff2";
import inter600 from "@fontsource/inter/files/inter-latin-600-normal.woff2";
import inter700 from "@fontsource/inter/files/inter-latin-700-normal.woff2";
import inter800 from "@fontsource/inter/files/inter-latin-800-normal.woff2";
import mono400 from "@fontsource/jetbrains-mono/files/jetbrains-mono-latin-400-normal.woff2";
import mono600 from "@fontsource/jetbrains-mono/files/jetbrains-mono-latin-600-normal.woff2";

// The website's fonts, bundled from npm (no font CDN needed at render time): Inter for text and headings,
// JetBrains Mono for code and extracted text. loadFont holds each frame until they are ready.
const faces: [string, string, string][] = [
  ["Inter", inter400, "400"],
  ["Inter", inter500, "500"],
  ["Inter", inter600, "600"],
  ["Inter", inter700, "700"],
  ["Inter", inter800, "800"],
  ["JetBrains Mono", mono400, "400"],
  ["JetBrains Mono", mono600, "600"],
];
for (const [family, url, weight] of faces) {
  loadFont({ family, url, weight, format: "woff2" });
}

export const fonts = {
  display: "Inter, sans-serif",
  sans: "Inter, sans-serif",
  mono: "'JetBrains Mono', monospace",
};
// The website's dark theme (docs/style.css), plus the three sources' colours.
export const colors = {
  stage: "#0b0c10",
  surface: "#16191f",
  paper: "#1d2128",
  paper2: "#232833",
  line: "#252a32",
  line2: "#323843",
  ink: "#e7e9ed",
  muted: "#99a1ad",
  raw: "#6c7480",
  accent: "#8b8ff8",
  accent2: "#c084fc",
  indigo: "#4f46e5",
  native: "#94a3b8",
  ocr: "#fbbf24",
  llm: "#a3a6fa",
  good: "#4ade80",
  bad: "#f87171",
  delBg: "rgba(248,113,113,.26)",
  insBg: "rgba(74,222,128,.22)",
  scan: "#efece3",
  scanInk: "#2b2a27",
  markFg: "#eef0ff",
};

export const FPS = 30;
export const WIDTH = 1280;
export const HEIGHT = 800;
