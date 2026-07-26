import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";

export const BackgroundAccents: React.FC = () => {
    const frame = useCurrentFrame();
    const drift = (frame * 0.15) % 200;

    return (
          <AbsoluteFill>
                <AbsoluteFill
                          style={{
                                      background:
                                                    "linear-gradient(to bottom, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0) 18%, rgba(0,0,0,0) 65%, rgba(0,0,0,0.65) 100%)",
                          }}
                        />
                <svg width="1080" height="1920" style={{ position: "absolute", top: 0, left: 0, opacity: 0.12 }}>
                  {Array.from({ length: 6 }).map((_, i) => (
                      <line
                                    key={i}
                                    x1={-100 + i * 220 + drift}
                                    y1={1920}
                                    x2={200 + i * 220 + drift}
                                    y2={1400}
                                    stroke="#00FF9C"
                                    strokeWidth={2}
                                  />
                    ))}
                </svg>
          </AbsoluteFill>
        );
};
