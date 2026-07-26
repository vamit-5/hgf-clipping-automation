import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";

type Word = { word: string; start: number; end: number };

const ACCENT = "#00FF9C";
const GROUP_MAX_WORDS = 4;
const GROUP_MAX_GAP = 0.6;

function groupWords(words: Word[]): Word[][] {
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

    return (
          <AbsoluteFill
                  style={{ justifyContent: "flex-end", alignItems: "center", paddingBottom: "22%" }}
                >
                <div
                          style={{
                                      display: "flex",
                                      flexWrap: "wrap",
                                      justifyContent: "center",
                                      maxWidth: "90%",
                                      gap: "0 14px",
                          }}
                        >
                  {activeGroup.map((w, i) => {
                                    const isActive = t >= w.start && t <= w.end;
                                    const wordStartFrame = w.start * fps;
                                    const scale = interpolate(frame, [wordStartFrame - 2, wordStartFrame + 4], [1.15, 1], {
                                                  extrapolateLeft: "clamp",
                                                  extrapolateRight: "clamp",
                                    });
                                    return (
                                                  <span
                                                                  key={i}
                                                                  style={{
                                                                                    display: "inline-block",
                                                                                    fontFamily: "Arial, sans-serif",
                                                                                    fontWeight: 700,
                                                                                    fontSize: 62,
                                                                                    color: isActive ? ACCENT : "#FFFFFF",
                                                                                    textShadow: "0 2px 10px rgba(0,0,0,0.6)",
                                                                                    transform: isActive ? `scale(${scale})` : "scale(1)",
                                                                                    textTransform: "uppercase",
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
