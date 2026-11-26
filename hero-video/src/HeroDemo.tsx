import type { FC } from "react";
import { AbsoluteFill, Series, useCurrentFrame } from "remotion";
import { CatchScene } from "./scenes/CatchScene";
import { ColdOpenScene } from "./scenes/ColdOpenScene";
import { MeasureScene } from "./scenes/MeasureScene";
import { MONTAGE_FRAMES, MontageScene } from "./scenes/MontageScene";
import { OutroScene } from "./scenes/OutroScene";
import { ThreeWaysScene } from "./scenes/ThreeWaysScene";
import { fonts } from "./theme";

const COLD_OPEN_FRAMES = 70;
const THREE_WAYS_FRAMES = 132;
const MEASURE_FRAMES = 138;
const CATCH_FRAMES = 120;
const OUTRO_FRAMES = 130;

export const HERO_FRAMES = COLD_OPEN_FRAMES + THREE_WAYS_FRAMES + MEASURE_FRAMES + CATCH_FRAMES + MONTAGE_FRAMES + OUTRO_FRAMES;

const GLOW_DRIFT_PIXELS = 120;
const GLOW_PERIOD_FRAMES = 240;
const DOT_DRIFT_PIXELS_PER_FRAME = 0.4;

const Stage: FC = () => {
  const frame = useCurrentFrame();
  const phase = (frame / GLOW_PERIOD_FRAMES) * Math.PI * 2;
  const indigoX = 900 + Math.cos(phase) * GLOW_DRIFT_PIXELS;
  const violetY = 600 + Math.sin(phase) * GLOW_DRIFT_PIXELS;
  return (
    <AbsoluteFill
      style={{
        backgroundColor: "#0b0c10",
        backgroundImage: [
          `radial-gradient(760px 520px at ${indigoX}px 150px, rgba(79,70,229,.28), transparent 70%)`,
          `radial-gradient(700px 480px at 160px ${violetY}px, rgba(192,132,252,.16), transparent 70%)`,
          "radial-gradient(rgba(231,233,237,.08) 1px, transparent 1.4px)",
        ].join(", "),
        backgroundSize: "auto, auto, 22px 22px",
        backgroundPosition: `0 0, 0 0, 0 ${-frame * DOT_DRIFT_PIXELS_PER_FRAME}px`,
      }}
    />
  );
};

export const HeroDemo: FC = () => (
  <AbsoluteFill style={{ fontFamily: fonts.sans, overflow: "hidden" }}>
    <Stage />
    <Series>
      <Series.Sequence durationInFrames={COLD_OPEN_FRAMES} name="Your pipeline read this">
        <ColdOpenScene />
      </Series.Sequence>
      <Series.Sequence durationInFrames={THREE_WAYS_FRAMES} name="Three ways to read a page">
        <ThreeWaysScene />
      </Series.Sequence>
      <Series.Sequence durationInFrames={MEASURE_FRAMES} name="Measure the gap">
        <MeasureScene />
      </Series.Sequence>
      <Series.Sequence durationInFrames={CATCH_FRAMES} name="Catch the quiet fixes">
        <CatchScene />
      </Series.Sequence>
      <Series.Sequence durationInFrames={MONTAGE_FRAMES} name="Every view">
        <MontageScene />
      </Series.Sequence>
      <Series.Sequence durationInFrames={OUTRO_FRAMES} name="Install">
        <OutroScene />
      </Series.Sequence>
    </Series>
  </AbsoluteFill>
);
