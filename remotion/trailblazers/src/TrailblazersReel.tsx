import React from "react";
import { AbsoluteFill, OffthreadVideo, Audio, staticFile, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
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

// Ken Burns efekat: spor, kontinuiran zum + horizontalno pomeranje kadra.
// Podkast je snimljen u 16:9 (dva govornika sede levo i desno od okruglog
// stola), a mi secemo u 9:16 (portret). Bez ovoga kadar je STATICNO centriran
// - sto znaci da cesto pada TACNO na praznu sredinu (sto stvara "prazan
// kadar" problem). Ovaj efekat sporo pomera fokus levo-centar-desno-centar
// u krugu, uz blagi kontinuirani zum, tako da retko "zaglavi" dugo na
// praznom delu, i dodaje kinematican, "premium" osecaj pokreta.
//
// NAPOMENA: ovo NIJE pravo prepoznavanje ko trenutno prica (to bi trazilo
// diarizaciju - poseban audio-analiza korak). Ovo je opsti, "slepi" pokret
// koji poboljsava osecaj i smanjuje sansu da kamera stane na prazninu, ali
// ne garantuje da je uvek fokusirana bas na osobu koja prica.
function useKenBurnsTransform(cycleSeconds: number, maxZoom: number, maxPanPercent: number) {
    const frame = useCurrentFrame();
    const { fps } = useVideoConfig();
    const t = frame / fps;

    // Kontinuirani, spori "disanje" izmedju leve i desne strane (sinusoida),
    // plus blagi kontinuirani zum-in tokom celog klipa.
    const cyclePos = (t % cycleSeconds) / cycleSeconds; // 0 -> 1 -> 0 ciklicno
    const panWave = Math.sin(cyclePos * Math.PI * 2); // -1 .. 1
    const panPercent = panWave * maxPanPercent;

    const zoomProgress = interpolate(t, [0, cycleSeconds * 3], [0, 1], {
        extrapolateRight: "clamp",
    });
    const scale = 1 + zoomProgress * (maxZoom - 1);

    return { scale, panPercent };
}

export const TrailblazersReel: React.FC<Props> = ({
    videoPath,
    bgMusicPath,
    bgMusicVolume,
    words,
}) => {
    const { scale, panPercent } = useKenBurnsTransform(9, 1.18, 6);

    return (
          <AbsoluteFill style={{ backgroundColor: "#000" }}>
                  <AbsoluteFill style={{ overflow: "hidden" }}>
                      <OffthreadVideo
                                src={staticFile(videoPath)}
                                style={{
                                    width: "100%",
                                    height: "100%",
                                    objectFit: "cover",
                                    transform: `scale(${scale}) translateX(${panPercent}%)`,
                                    transformOrigin: "center center",
                                }}
                              />
                  </AbsoluteFill>
                  <BackgroundAccents />
                  <Branding />
                  <Captions words={words} />
            {bgMusicPath ? (
                    <Audio src={staticFile(bgMusicPath)} volume={bgMusicVolume} loop />
                  ) : null}
          </AbsoluteFill>
        );
};
