import type { FC } from "react";
import { AbsoluteFill, Img, Series, staticFile, useCurrentFrame } from "remotion";
import { Flash, gradientText, progress, Slam } from "../motion";
import { colors, fonts } from "../theme";

export const CUT_FRAMES = 26;

type Cut = { view: string; question: string; still: string };

// The last frames of the docs' tours (docs/tours/*-dark.png, made by .claude/skills/demo/tour_shots.py).
const CUTS: Cut[] = [
  { view: "Page inspector", question: "What did OCR miss on this page?", still: "inspector" },
  { view: "Compare", question: "Which page reads worst?", still: "compare" },
  { view: "Search", question: "Where is NB-0417 mentioned?", still: "search" },
  { view: "Library", question: "Which pages still have no text?", still: "library" },
  { view: "Extract", question: "OCR a new upload, in the background", still: "upload" },
];

export const MONTAGE_FRAMES = CUTS.length * CUT_FRAMES;

const Still: FC<{ name: string; leansLeft: boolean }> = ({ name, leansLeft }) => {
  const frame = useCurrentFrame();
  const side = leansLeft ? -1 : 1;
  const arrived = progress(frame, 0, 10);
  return (
    <div style={{ position: "absolute", left: 110, right: 110, top: 200, height: 700, perspective: 1500 }}>
      <Img
        src={staticFile(`tours/${name}-dark.png`)}
        style={{
          width: "100%",
          borderRadius: 16,
          border: `1px solid ${colors.line2}`,
          boxShadow: "0 40px 90px rgba(0,0,0,.75), 0 0 70px rgba(139,143,248,.25)",
          transform: `translateX(${side * (1 - arrived) * 420}px) rotateY(${side * -(10 + (1 - arrived) * 22)}deg) rotateX(6deg) scale(${1 + frame * 0.002})`,
          transformOrigin: "top center",
        }}
      />
    </div>
  );
};

const Dots: FC<{ current: number }> = ({ current }) => (
  <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
    {CUTS.map((cut, i) => (
      <div key={cut.still} style={{ width: 14, height: 14, borderRadius: "50%", background: i <= current ? colors.accent : colors.line2 }} />
    ))}
  </div>
);

const CutScene: FC<{ cut: Cut; index: number }> = ({ cut, index }) => (
  <AbsoluteFill style={{ overflow: "hidden" }}>
    <Still name={cut.still} leansLeft={index % 2 === 0} />
    <AbsoluteFill style={{ background: "linear-gradient(rgba(11,12,16,.98) 22%, transparent 40%, transparent 76%, rgba(11,12,16,.95))" }} />
    <div style={{ position: "absolute", left: 48, right: 48, top: 30 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 10 }}>
        <span style={{ fontSize: 28, fontWeight: 600, color: colors.muted }}>{cut.view}</span>
        <Dots current={index} />
      </div>
      <Slam at={1} from={1.25} style={{ fontFamily: fonts.display, fontSize: 60, fontWeight: 800, letterSpacing: "-.03em", lineHeight: 1.05, transformOrigin: "left center", ...gradientText }}>
        {cut.question}
      </Slam>
    </div>
    <Flash at={0} peak={0.25} />
  </AbsoluteFill>
);

export const MontageScene: FC = () => (
  <Series>
    {CUTS.map((cut, index) => (
      <Series.Sequence key={cut.still} durationInFrames={CUT_FRAMES} name={cut.view} premountFor={CUT_FRAMES}>
        <CutScene cut={cut} index={index} />
      </Series.Sequence>
    ))}
  </Series>
);
