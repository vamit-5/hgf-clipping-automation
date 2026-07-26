import React from "react";
import { AbsoluteFill, OffthreadVideo, Audio } from "remotion";
import { z } from "zod";
import { Captions } from "./Captions";
import { Branding } from "./Branding";
import { BackgroundAccents } from "./BackgroundAccents";

export const wordSchema = z.object({
    word: z.string(),
    start: z.number(),
    end: z.number(),
});

export const trailblazersReelSchema = z.object({
    videoPath: z.string(),
    bgMusicPath: z.string().optional(),
    bgMusicVolume: z.number().default(0.45),
    durationInSeconds: z.number(),
    words: z.array(wordSchema),
});

type Props = z.infer<typeof trailblazersReelSchema>;

export const TrailblazersReel: React.FC<Props> = ({
    videoPath,
    bgMusicPath,
    bgMusicVolume,
    words,
}) => {
    return (
          <AbsoluteFill style={{ backgroundColor: "#000" }}>
                  <OffthreadVideo
                            src={videoPath}
                            style={{ width: "100%", height: "100%", objectFit: "cover" }}
                          />
                  <BackgroundAccents />
                  <Branding />
                  <Captions words={words} />
            {bgMusicPath ? (
                    <Audio src={bgMusicPath} volume={bgMusicVolume} loop />
                  ) : null}
          </AbsoluteFill>
        );
};
