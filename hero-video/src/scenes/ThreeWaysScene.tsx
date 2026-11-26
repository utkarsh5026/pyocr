import type { FC } from "react";
import { useCurrentFrame } from "remotion";
import { Burst, progress, Rise, Scene, Slam } from "../motion";
import { colors, fonts } from "../theme";
import { card, monoText, Pill } from "../ui";

const SCAN_AT = 10;
const SCAN_FRAMES = 26;
const NATIVE_AT = 22;
const OCR_AT = 46;
const LLM_AT = 72;

const NOTE_LINES = [
  "FIELD NOTES - SITE 7, NORTH PIER",
  "Date: 14 Aug 2025",
  "",
  "09:10  Arrived on site.",
  "09:25  Sensor NB-0417/ii found",
  "       tilted ~4 deg. Re-seated",
  "10:05  Sampled water at buoys",
  "       B1, B2, B4 (B3 missing).",
  "10:40  Photo set 0193-0221",
];

// A scanned page: a photo of paper, so it has pixels and no text.
const ScannedPage: FC = () => {
  const frame = useCurrentFrame();
  const scanned = progress(frame, SCAN_AT, SCAN_AT + SCAN_FRAMES);
  const isScanning = frame >= SCAN_AT && frame < SCAN_AT + SCAN_FRAMES;
  return (
    <div
      style={{
        position: "relative",
        width: 400,
        height: 540,
        background: colors.scan,
        borderRadius: 6,
        rotate: "-1.2deg",
        padding: "40px 30px",
        boxShadow: "0 30px 70px rgba(0,0,0,.7)",
        overflow: "hidden",
        fontFamily: "Courier New, monospace",
        fontSize: 15.5,
        lineHeight: 1.75,
        color: colors.scanInk,
        filter: "blur(.35px)",
      }}
    >
      {NOTE_LINES.map((line, i) => (
        <div key={i} style={{ whiteSpace: "pre", minHeight: "1em" }}>
          {line}
        </div>
      ))}
      <div style={{ position: "absolute", left: 18, top: 14, fontFamily: fonts.sans, fontSize: 15, fontWeight: 700, color: "#b45309", background: "#fdf3e3", borderRadius: 999, padding: "2px 12px", filter: "none" }}>
        scan
      </div>
      {isScanning ? (
        <div style={{ position: "absolute", left: 0, right: 0, top: `${scanned * 100}%`, height: 5, background: colors.accent, boxShadow: `0 0 30px 10px ${colors.accent}` }} />
      ) : null}
    </div>
  );
};

const Row: FC<{ at: number; source: "native" | "ocr" | "llm"; note: string; children: React.ReactNode }> = ({ at, source, note, children }) => (
  <Slam at={at} from={1.25} style={{ ...card({ padding: "16px 22px" }), transformOrigin: "left center" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 10 }}>
      <Pill source={source} />
      <span style={{ fontSize: 20, color: colors.muted }}>{note}</span>
    </div>
    <div style={{ ...monoText, fontSize: 23, lineHeight: 1.45 }}>{children}</div>
  </Slam>
);

const Mark: FC<{ at: number; color: string; children: React.ReactNode }> = ({ at, color, children }) => {
  const frame = useCurrentFrame();
  const swept = progress(frame, at, at + 10) * 100;
  return (
    <span
      style={{
        borderRadius: 5,
        padding: "0 4px",
        backgroundImage: `linear-gradient(${color}, ${color})`,
        backgroundRepeat: "no-repeat",
        backgroundSize: `${swept}% 100%`,
        boxShadow: swept > 99 ? `0 0 0 2px ${color}` : undefined,
      }}
    >
      {children}
    </span>
  );
};

export const ThreeWaysScene: FC = () => (
  <Scene lead="Three ways to" accent="read a page.">
    <div style={{ position: "absolute", inset: 0, display: "flex", gap: 46, alignItems: "center" }}>
      <Rise at={2}>
        <ScannedPage />
      </Rise>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20 }}>
        <Row at={NATIVE_AT} source="native" note="what the file contains">
          <span style={{ color: colors.raw }}>0 characters. It's a picture.</span>
        </Row>
        <Row at={OCR_AT} source="ocr" note="pixels → letters">
          {"10:05 Sampled water at\nbuoys "}
          <Mark at={OCR_AT + 14} color="rgba(248,113,113,.4)">Bl</Mark>
          {", B2, B4 (B3 missing)."}
        </Row>
        <Row at={LLM_AT} source="llm" note="a vision model, told to copy">
          {"10:05 Sampled water at\nbuoys "}
          <Mark at={LLM_AT + 14} color="rgba(74,222,128,.35)">B1</Mark>
          {", B2, B4 (B3 missing)."}
        </Row>
      </div>
    </div>
    <Burst at={LLM_AT} color={colors.llm} x={860} y={600} count={26} reach={380} />
  </Scene>
);
