import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring } from "remotion";

const ACCENT = "#00FF9C";

export const Branding: React.FC = () => {
    const frame = useCurrentFrame();
    const { fps } = useVideoConfig();

    const markRotate = interpolate(frame, [0, 45], [-25, 0], { extrapolateRight: "clamp" });
    const markScale = spring({ frame, fps, config: { damping: 12 }, durationInFrames: 20 });
    const lineWidth = interpolate(frame, [10, 35], [0, 100], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });
    const titleOpacity = interpolate(frame, [15, 30], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });

    return (
          <AbsoluteFill style={{ alignItems: "center", paddingTop: 70 }}>
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
                            <div
                                        style={{
                                                      width: 54,
                                                      height: 54,
                                                      borderRadius: "50%",
                                                      border: `3px solid ${ACCENT}`,
                                                      display: "flex",
                                                      alignItems: "center",
                                                      justifyContent: "center",
                                                      transform: `rotate(${markRotate}deg) scale(${markScale})`,
                                                      fontFamily: "Arial, sans-serif",
                                                      fontWeight: 700,
                                                      fontSize: 20,
                                                      color: ACCENT,
                                        }}
                                      >
                                      TB
                            </div>
                          <div style={{ opacity: titleOpacity, textAlign: "center" }}>
                                    <div
                                                  style={{
                                                                  fontFamily: "Arial, sans-serif",
                                                                  fontWeight: 700,
                                                                  fontSize: 30,
                                                                  color: "#FFFFFF",
                                                                  letterSpacing: 2,
                                                                  textShadow: "0 2px 8px rgba(0,0,0,0.6)",
                                                  }}
                                                >
                                                TRAILBLAZERS PODCAST
                                    </div>
                                    <div
                                                  style={{ fontFamily: "Arial, sans-serif", fontWeight: 400, fontSize: 20, color: ACCENT, marginTop: 2 }}
                                                >
                                                Ann Miura-Ko
                                    </div>
                                    <div style={{ height: 2, width: `${lineWidth}%`, background: ACCENT, margin: "6px auto 0" }} />
                          </div>
                  </div>
          </AbsoluteFill>
        );
};
