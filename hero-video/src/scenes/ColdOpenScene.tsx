import type { FC } from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { Burst, Flash, Punch, Shake, Slam } from "../motion";
import { colors, fonts } from "../theme";

// What "just extract the text" really hands a pipeline: broken font maps, split words, mojibake, empty scans.
const RAW_LINES = [
  "Rev enue for the qu arter w as $4.8 2M, up 1 1% on Q2.",
  "(cid:37)(cid:72)(cid:85)(cid:72)(cid:3)(cid:76)(cid:86)(cid:3)",
  "Orders recie ved in Sep- tember alone reached 1,2 04",
  "Ã©tÃ© â€\" rÃ©sumÃ© Â· Â§ 4.2 â€œfirmwareâ€",
  "[page 2: no text layer]",
  "Sampled water at buoys Bl, B2, B4 (B3 rnissing).",
  "NB-0417/i i  sensor   line   ...   .   ,   :",
  "􏰀􏰁􏰂􏰃 􏰄􏰅􏰆 Line Q1 Q2 Q3 Change NB-0417 1,210 1,342",
  "[page 3: no text layer]",
  "12:00\"> iInch. 13220 Found 3 cm crack in",
];
const WALL_LINES = 70;
const LINE_PIXELS = 34;
const SCROLL_PIXELS_PER_FRAME = 10;
const THIS_AT = 28;

const Wall: FC = () => {
  const frame = useCurrentFrame();
  const lines = Array.from({ length: WALL_LINES }, (_, i) => RAW_LINES[i % RAW_LINES.length]);
  return (
    <div
      style={{
        position: "absolute",
        inset: "-260px -300px",
        rotate: "-8deg",
        fontFamily: fonts.mono,
        fontSize: 24,
        lineHeight: `${LINE_PIXELS}px`,
        whiteSpace: "nowrap",
        translate: `0 ${-frame * SCROLL_PIXELS_PER_FRAME}px`,
      }}
    >
      {lines.map((line, i) => (
        <div key={i} style={{ color: line.startsWith("[") || line.includes("cid") ? colors.bad : colors.raw, paddingLeft: (i * 37) % 170 }}>
          {line} &nbsp; {RAW_LINES[(i + 4) % RAW_LINES.length]}
        </div>
      ))}
    </div>
  );
};

export const ColdOpenScene: FC = () => (
  <AbsoluteFill style={{ overflow: "hidden" }}>
    <Punch hits={[THIS_AT]} amount={0.09}>
      <Shake hits={[THIS_AT]} strength={28}>
        <Wall />
        <AbsoluteFill style={{ background: "radial-gradient(ellipse at center, rgba(11,12,16,.95) 28%, rgba(11,12,16,.35) 82%)" }} />
        <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: fonts.display, fontWeight: 800, letterSpacing: "-.04em", textAlign: "center" }}>
          <Slam at={4} from={2} style={{ fontSize: 92, color: colors.ink }}>
            Your pipeline read
          </Slam>
          <Slam at={THIS_AT} from={3.2} style={{ fontSize: 230, lineHeight: 1, color: colors.bad, textShadow: "0 0 70px rgba(248,113,113,.55)" }}>
            this.
          </Slam>
        </AbsoluteFill>
      </Shake>
    </Punch>
    <Flash at={THIS_AT} color={colors.bad} peak={0.5} />
    <Burst at={THIS_AT} color={colors.bad} x={640} y={470} />
  </AbsoluteFill>
);
