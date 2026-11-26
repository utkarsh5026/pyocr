import type { FC } from "react";
import { AbsoluteFill } from "remotion";
import { Burst, Flash, Rise, Scene, Slam, Sweep } from "../motion";
import { colors } from "../theme";
import { card, Del, Ins, monoText, Pill } from "../ui";

const NATIVE_AT = 4;
const LLM_AT = 22;
const DIFF_AT = 46;
const VERDICT_AT = 72;

// harbor-q3-report.pdf really says "recieved"; the stand-in model, like real ones, quietly fixes it.
export const CatchScene: FC = () => (
  <AbsoluteFill>
    <Scene lead="Catch the" accent="quiet fixes." hits={[DIFF_AT]}>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", justifyContent: "center", gap: 18 }}>
        <Rise at={NATIVE_AT} style={card()}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 10 }}>
            <Pill source="native" />
            <span style={{ fontSize: 20, color: colors.muted }}>what the PDF really says</span>
          </div>
          <div style={{ ...monoText, fontSize: 25 }}>Orders recieved in September alone reached 1,204</div>
        </Rise>
        <Rise at={LLM_AT} style={card()}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 10 }}>
            <Pill source="llm" />
            <span style={{ fontSize: 20, color: colors.muted }}>told to copy it verbatim</span>
          </div>
          <div style={{ ...monoText, fontSize: 25 }}>Orders received in September alone reached 1,204</div>
        </Rise>
        <Slam at={DIFF_AT} from={1.3} style={{ ...card({ border: `2px solid ${colors.accent}`, boxShadow: "0 0 60px rgba(139,143,248,.35)" }), transformOrigin: "left center" }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: colors.accent, marginBottom: 10 }}>pyocr diff</div>
          <div style={{ ...monoText, fontSize: 27 }}>
            {"Orders "}
            <Del>recieved</Del> <Ins>received</Ins>
            {" in September"}
          </div>
        </Slam>
        <Rise at={VERDICT_AT} style={{ fontSize: 36, fontWeight: 700, color: colors.ink, marginTop: 6 }}>
          The model fixed a typo{" "}
          <Sweep at={VERDICT_AT + 6} frames={16}>
            that's really in the file.
          </Sweep>
        </Rise>
      </div>
    </Scene>
    <Flash at={DIFF_AT} color={colors.accent} peak={0.3} />
    <Burst at={DIFF_AT} color={colors.accent} x={420} y={520} count={30} reach={420} />
  </AbsoluteFill>
);
