import React from "react";
import { Composition } from "remotion";
import { TrailblazersReel, trailblazersReelSchema } from "./TrailblazersReel";

const FPS = 30;

export const RemotionRoot: React.FC = () => {
    return (
          <Composition
                  id="TrailblazersReel"
                  component={TrailblazersReel}
                  durationInFrames={900}
                  fps={FPS}
                  width={1080}
                  height={1920}
                  schema={trailblazersReelSchema}
                  defaultProps={{
                            videoPath: "",
                            bgMusicPath: "",
                            bgMusicVolume: 0.45,
                            durationInSeconds: 30,
                            words: [],
                  }}
                  calculateMetadata={async ({ props }) => {
                            return {
                                        durationInFrames: Math.max(1, Math.round(props.durationInSeconds * FPS)),
                            };
                  }}
                />
        );
};
