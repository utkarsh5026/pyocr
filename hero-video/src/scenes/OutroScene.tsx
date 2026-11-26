import type { FC } from "react";
import { AbsoluteFill, Img, staticFile } from "remotion";
import { Burst, Flash, gradientText, Pop, Ring, Rise, Shake, Slam, Typed } from "../motion";
import { colors, fonts } from "../theme";
import { Pill } from "../ui";

const TITLE_AT = 14;
const PILLS = ["Runs locally", "Open source", "Claude · GPT · Gemini · local models"];

export const OutroScene: FC = () => (
  <AbsoluteFill>
    <Shake hits={[TITLE_AT]} strength={16}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", textAlign: "center", padding: "0 60px" }}>
        <Pop at={0}>
          <Img src={staticFile("logo.svg")} style={{ width: 92, height: 92, display: "block", marginBottom: 26, boxShadow: "0 0 60px rgba(139,143,248,.5)", borderRadius: 22 }} />
        </Pop>
        <div style={{ display: "flex", gap: 12, marginBottom: 30 }}>
          <Rise at={4}>
            <Pill source="native" size={20} />
          </Rise>
          <Rise at={7}>
            <Pill source="ocr" size={20} />
          </Rise>
          <Rise at={10}>
            <Pill source="llm" size={20} />
          </Rise>
        </div>
        <Slam at={TITLE_AT} from={2.1} style={{ fontFamily: fonts.display, fontWeight: 800, fontSize: 96, lineHeight: 1.04, letterSpacing: "-.04em", color: colors.ink, marginBottom: 40 }}>
          See what your
          <br />
          pipeline <span style={gradientText}>really read.</span>
        </Slam>
        <Pop at={TITLE_AT + 14}>
          <div style={{ fontFamily: fonts.mono, fontSize: 30, color: colors.ink, background: colors.surface, border: `2px solid ${colors.accent}`, borderRadius: 14, padding: "16px 30px", boxShadow: "0 0 70px rgba(139,143,248,.4)", minWidth: 900, textAlign: "left" }}>
            <span style={{ color: colors.accent }}>$ </span>
            <Typed text="pipx install git+https://github.com/utkarsh5026/pyocr" at={TITLE_AT + 18} framesPerChar={0.45} />
          </div>
        </Pop>
        <div style={{ display: "flex", gap: 14, marginTop: 30 }}>
          {PILLS.map((pill, i) => (
            <Rise key={pill} at={TITLE_AT + 46 + i * 4} style={{ fontSize: 24, fontWeight: 600, color: colors.ink, background: colors.paper2, border: `1px solid ${colors.line2}`, borderRadius: 999, padding: "8px 22px" }}>
              {pill}
            </Rise>
          ))}
        </div>
      </AbsoluteFill>
    </Shake>
    <Flash at={TITLE_AT} peak={0.5} />
    <Ring at={TITLE_AT} color={colors.accent} x={640} y={380} />
    <Burst at={TITLE_AT} color={colors.accent} x={640} y={380} count={60} reach={720} />
  </AbsoluteFill>
);
