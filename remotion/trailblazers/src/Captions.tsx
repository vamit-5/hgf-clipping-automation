import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";

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

    // Pop-in animacija: SAMO fade + blag pomak nagore (bez scale-a!). Scale
    // na tekstu je izazivao vidljivo "stiskanje" (izoblicenje) slova tokom
    // animacije - opacity+translateY nikad ne izoblicuje oblik slova.
    const groupStartFrame = activeGroup[0].start * fps;
    const groupOpacity = interpolate(frame, [groupStartFrame, groupStartFrame + 5], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });
    const groupTranslateY = interpolate(frame, [groupStartFrame, groupStartFrame + 6], [18, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });

    return (
          <AbsoluteFill
                  style={{ justifyContent: "flex-end", alignItems: "center", paddingBottom: "38%" }}
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
                                      transform: `translateY(${groupTranslateY}px)`,
                                      opacity: groupOpacity,
                          }}
                        >
                  {activeGroup.map((w, i) => {
                                    const isActive = t >= w.start && t <= w.end;
                                    // Aktivna rec se istice SAMO bojom/pozadinom - bez scale-a
                                    // (scale je izazivao vidljivo stiskanje/izoblicenje slova).
                                    return (
                                                  <span
                                                                  key={i}
                                                                  style={{
                                                                                    display: "inline-block",
                                                                                    fontFamily: "'Arial Black', Arial, sans-serif",
                                                                                    fontWeight: 900,
                                                                                    fontSize: 60,
                                                                                    color: isActive ? "#0A0A0A" : "#FFFFFF",
                                                                                    backgroundColor: isActive ? ACCENT : "transparent",
                                                                                    padding: isActive ? "2px 10px" : "2px 0",
                                                                                    borderRadius: 8,
                                                                                    textShadow: isActive ? "none" : "0 2px 10px rgba(0,0,0,0.8)",
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
