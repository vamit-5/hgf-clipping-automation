import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring } from "remotion";

export type Word = { word: string; start: number; end: number };

const ACCENT = "#00FF9C";
const GROUP_MAX_WORDS = 4;
const GROUP_MAX_GAP = 0.6;

// Izvezeno da bi TrailblazersReel.tsx mogao da koristi ISTU logiku grupisanja
// za sinhronizaciju rezova kamere sa promenama titlova.
export function groupWords(words: Word[]): Word[][] {
    const groups: Word[][] = [];
    let current: Word[] = [];
    for (const w of words) {
          if (
                  current.length &&
                  (w.start - current[current.length - 1].end > GROUP_MAX_GAP ||
                           current.length >= GROUP_MAX_WORDS)
                ) {
                  groups.push(current);
                  current = [];
          }
          current.push(w);
    }
    if (current.length) groups.push(current);
    return groups;
}

export const Captions: React.FC<{ words: Word[] }> = ({ words }) => {
    const frame = useCurrentFrame();
    const { fps } = useVideoConfig();
    const t = frame / fps;
    const groups = groupWords(words);

    const activeGroup = groups.find(
          (g) => t >= g[0].start - 0.05 && t <= g[g.length - 1].end + 0.15
        );
    if (!activeGroup) return null;

    // Pop-in animacija: grupa "iskoci" (scale + fade) kad se prvi put pojavi.
    const groupStartFrame = activeGroup[0].start * fps;
    const popIn = spring({
          frame: frame - groupStartFrame,
          fps,
          config: { damping: 11, stiffness: 180, mass: 0.5 },
          durationInFrames: 12,
    });
    const groupScale = interpolate(popIn, [0, 1], [0.85, 1]);
    const groupOpacity = interpolate(frame, [groupStartFrame, groupStartFrame + 4], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });

    return (
          <AbsoluteFill
                  style={{ justifyContent: "flex-end", alignItems: "center", paddingBottom: "32%" }}
                >
                <div
                          style={{
                                      display: "flex",
                                      flexWrap: "wrap",
                                      justifyContent: "center",
                                      maxWidth: "88%",
                                      gap: "10px 10px",
                                      backgroundColor: "rgba(0,0,0,0.55)",
                                      border: `2px solid ${ACCENT}`,
                                      borderRadius: 20,
                                      padding: "14px 26px",
                                      boxShadow: "0 8px 30px rgba(0,0,0,0.5)",
                                      transform: `scale(${groupScale})`,
                                      opacity: groupOpacity,
                          }}
                        >
                  {activeGroup.map((w, i) => {
                                    const isActive = t >= w.start && t <= w.end;
                                    const wordStartFrame = w.start * fps;
                                    const scale = interpolate(frame, [wordStartFrame - 2, wordStartFrame + 4], [1.25, 1], {
                                                  extrapolateLeft: "clamp",
                                                  extrapolateRight: "clamp",
                                    });
                                    return (
                                                  <span
                                                                  key={i}
                                                                  style={{
                                                                                    display: "inline-block",
                                                                                    fontFamily: "Arial, sans-serif",
                                                                                    fontWeight: 900,
                                                                                    fontSize: 60,
                                                                                    color: isActive ? "#0A0A0A" : "#FFFFFF",
                                                                                    backgroundColor: isActive ? ACCENT : "transparent",
                                                                                    padding: isActive ? "2px 10px" : "2px 0",
                                                                                    borderRadius: 8,
                                                                                    textShadow: isActive ? "none" : "0 2px 10px rgba(0,0,0,0.8)",
                                                                                    transform: `scale(${scale})`,
                                                                                    textTransform: "uppercase",
                                                                                    letterSpacing: 0.5,
                                                                  }}
                                                                >
                                                    {w.word}
                                                  </span>
                                                );
                        })}
                </div>
          </AbsoluteFill>
        );
};
