import type { CSSProperties, FC, ReactNode } from "react";
import { colors, fonts } from "./theme";

export type Source = "native" | "ocr" | "llm";

const SOURCE: Record<Source, { label: string; color: string; wash: string }> = {
  native: { label: "Native text", color: colors.native, wash: "rgba(148,163,184,.14)" },
  ocr: { label: "Tesseract OCR", color: colors.ocr, wash: "rgba(251,191,36,.13)" },
  llm: { label: "LLM transcription", color: colors.llm, wash: "rgba(139,143,248,.16)" },
};

// The website's source pill: a coloured dot and the source's name.
export const Pill: FC<{ source: Source; size?: number }> = ({ source, size = 22 }) => {
  const s = SOURCE[source];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        fontSize: size,
        fontWeight: 700,
        color: s.color,
        background: s.wash,
        border: `1px solid ${s.color}55`,
        borderRadius: 999,
        padding: "5px 16px",
      }}
    >
      <span style={{ width: size * 0.45, height: size * 0.45, borderRadius: "50%", background: s.color }} />
      {s.label}
    </span>
  );
};

export const Del: FC<{ children: ReactNode }> = ({ children }) => (
  <span style={{ background: colors.delBg, color: "#fecaca", textDecoration: "line-through", borderRadius: 4, padding: "0 3px" }}>{children}</span>
);

export const Ins: FC<{ children: ReactNode }> = ({ children }) => (
  <span style={{ background: colors.insBg, color: "#bbf7d0", borderRadius: 4, padding: "0 3px" }}>{children}</span>
);

export const card = (extra: CSSProperties = {}): CSSProperties => ({
  background: colors.surface,
  border: `1px solid ${colors.line2}`,
  borderRadius: 18,
  padding: "18px 24px",
  boxShadow: "0 24px 60px -12px rgba(0,0,0,.65)",
  ...extra,
});

export const monoText: CSSProperties = { fontFamily: fonts.mono, color: colors.ink, whiteSpace: "pre" };
