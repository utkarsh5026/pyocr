import type { FC } from "react";
import { useCurrentFrame } from "remotion";
import { Burst, Pop, progress, Rise, Ring, Scene, Slam } from "../motion";
import { colors, fonts } from "../theme";
import { card, Del, Ins, monoText } from "../ui";

// Tesseract scored against the LLM on the demo scan (field-notes-scan.pdf), as the Compare view reports it.
// Page 3 has no LLM text in the demo data yet, so it is shown but not scored.
export const PAGE_CER: (number | null)[] = [0.08, 0.093, null];
export const DOC_CER = 0.086;
export const DOC_WER = 0.271;

const TILES_AT = 6;
const BARS_AT = 26;
const WORST_AT = 58;
const DIFF_AT = 70;

const errorColor = (rate: number) => (rate <= 0.02 ? "#60a5fa" : rate <= 0.1 ? colors.ocr : colors.bad);

const Counter: FC<{ to: number; at: number; color: string; label: string }> = ({ to, at, color, label }) => {
  const frame = useCurrentFrame();
  const shown = to * progress(frame, at, at + 22);
  return (
    <Rise at={at} style={{ ...card({ padding: "14px 22px" }), flex: 1 }}>
      <div style={{ fontSize: 17, fontWeight: 700, letterSpacing: ".08em", color: colors.muted, textTransform: "uppercase" }}>{label}</div>
      <div style={{ fontFamily: fonts.display, fontSize: 60, fontWeight: 800, color, letterSpacing: "-.02em", lineHeight: 1.1 }}>{(shown * 100).toFixed(1)}%</div>
    </Rise>
  );
};

const Bars: FC = () => {
  const frame = useCurrentFrame();
  const max = Math.max(...PAGE_CER.map((rate) => rate ?? 0));
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 18, padding: "0 4px" }}>
      {PAGE_CER.map((rate, i) => {
        const grown = progress(frame, BARS_AT + i * 5, BARS_AT + i * 5 + 16);
        const isWorst = rate === max;
        if (rate === null) {
          return (
            <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
              <div
                style={{
                  width: 64,
                  height: 150 * grown,
                  borderRadius: "8px 8px 3px 3px",
                  border: `1px dashed ${colors.line2}`,
                  backgroundImage: `repeating-linear-gradient(45deg, ${colors.line2} 0 2px, transparent 2px 9px)`,
                }}
              />
              <span style={{ fontSize: 18, color: colors.muted, fontWeight: 600 }}>p{i + 1}</span>
              <span style={{ fontSize: 15, color: colors.muted, height: 25, whiteSpace: "nowrap" }}>not scored</span>
            </div>
          );
        }
        const isPicked = isWorst && frame >= WORST_AT;
        return (
          <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 64,
                height: 150 * Math.sqrt(rate / max) * grown,
                background: errorColor(rate),
                borderRadius: "8px 8px 3px 3px",
                outline: isPicked ? `3px solid ${colors.ink}` : "none",
                outlineOffset: 3,
              }}
            />
            <span style={{ fontSize: 18, color: isPicked ? colors.ink : colors.muted, fontWeight: 600 }}>p{i + 1}</span>
            {isWorst ? (
              <Pop at={WORST_AT} style={{ fontSize: 16, fontWeight: 700, color: colors.stage, background: colors.ink, borderRadius: 7, padding: "3px 9px", whiteSpace: "nowrap" }}>
                worst page
              </Pop>
            ) : (
              <span style={{ height: 25 }} />
            )}
          </div>
        );
      })}
    </div>
  );
};

export const MeasureScene: FC = () => (
  <Scene lead="Measure" accent="the gap." hits={[WORST_AT]}>
    <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", justifyContent: "center", gap: 18 }}>
      <div style={{ display: "flex", gap: 18 }}>
        <Counter label="Character error" to={DOC_CER} at={TILES_AT} color={colors.ocr} />
        <Counter label="Word error" to={DOC_WER} at={TILES_AT + 4} color={colors.bad} />
        <Rise at={TILES_AT + 8} style={{ ...card({ padding: "14px 22px" }), flex: 1.2 }}>
          <div style={{ fontSize: 17, fontWeight: 700, letterSpacing: ".08em", color: colors.muted, textTransform: "uppercase" }}>OCR vs LLM</div>
          <div style={{ fontSize: 24, color: colors.ink, lineHeight: 1.35, marginTop: 8 }}>
            field-notes-scan.pdf
            <br />
            <span style={{ color: colors.muted }}>2 of 3 pages scored</span>
          </div>
        </Rise>
      </div>
      <div style={{ display: "flex", gap: 18, height: 310 }}>
        <Rise at={BARS_AT - 4} style={{ ...card(), width: 330 }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: colors.ink, marginBottom: 14 }}>Error rate per page</div>
          <Bars />
        </Rise>
        <Slam at={DIFF_AT} from={1.2} style={{ ...card(), flex: 1, transformOrigin: "left center" }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: colors.ink, marginBottom: 12 }}>
            Page 2 <span style={{ color: colors.muted, fontWeight: 500 }}>· where OCR differs</span>
          </div>
          <div style={{ ...monoText, fontSize: 22, lineHeight: 1.75 }}>
            {"11:30  Firmware on logger reads 3.8.2\n12:00  "}
            <Del>Lunch.</Del> <Ins>°&gt; iIninch.</Ins>
            {"\n"}
            <Del>13:20</Del> <Ins>13220</Ins>
            {"  Found 3 cm crack in concrete\nReadings (mg/L): "}
            <Del>B1</Del> <Ins>Bl</Ins>
            {" 7.9  B2 8.1  B4 6.4"}
          </div>
        </Slam>
      </div>
    </div>
    <Ring at={WORST_AT} color={colors.ocr} x={200} y={500} />
    <Burst at={DIFF_AT} color={colors.ocr} x={800} y={540} count={24} reach={360} />
  </Scene>
);
