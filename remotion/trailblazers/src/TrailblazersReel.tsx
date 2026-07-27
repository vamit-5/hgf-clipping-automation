import React from "react";
import {
    AbsoluteFill,
    staticFile,
    useCurrentFrame,
    useVideoConfig,
    interpolate,
} from "remotion";
// Video/Audio idu iz @remotion/media (noviji, pouzdaniji mehanizam za
// citanje frejmova - Mediabunny/WebCodecs) umesto OffthreadVideo iz
// "remotion" paketa, koji je izazivao "No frame found at position X" greske.
import { Video, Audio } from "@remotion/media";
import { z } from "zod";
import { Captions, groupWords, Word } from "./Captions";
import { Branding } from "./Branding";
import { BackgroundAccents } from "./BackgroundAccents";

export const wordSchema = z.object({
    word: z.string(),
    start: z.number(),
    end: z.number(),
});

// Rezultat PRAVE detekcije lica iz Pythona (analyze_face_positions u
// trailblazers_new_reel.py) - xFrac/wFrac su izmerene, STVARNE vrednosti
// (0-1, udeo sirine kadra), NE nagadjanje.
export const facePositionSchema = z.object({
    t: z.number(),
    xFrac: z.number(),
    wFrac: z.number(),
});

export const trailblazersReelSchema = z.object({
    videoPath: z.string(),
    bgMusicPath: z.string().optional(),
    bgMusicVolume: z.number().default(0.45),
    durationInSeconds: z.number(),
    words: z.array(wordSchema),
    facePositions: z.array(facePositionSchema).optional().default([]),
});

type Props = z.infer<typeof trailblazersReelSchema>;
type FaceSample = z.infer<typeof facePositionSchema>;

// Izvorni podkast je vec gotov multi-cam montiran snimak u 16:9 - kamera se
// U SAMOM IZVORU menja izmedju solo krupnog kadra osobe A, solo krupnog
// kadra osobe B, i sireg kadra sa obe osobe. Zato NE postoji fiksna pozicija
// koja uvek radi - kod mora da "vidi" gde je lice u svakom trenutku.
// Python (analyze_face_positions) vec je analizirao STVARAN isecen klip
// frame-po-frame (OpenCV detekcija lica) i izmerio pravu poziciju. Ovde
// samo koristimo ta izmerena merenja - NEMA vise nagadjanja/fiksnih brojeva.
const CANVAS_WIDTH = 1080;
const CANVAS_HEIGHT = 1920;
const SOURCE_ASPECT = 16 / 9;
const RENDERED_VIDEO_WIDTH = CANVAS_HEIGHT * SOURCE_ASPECT; // ~3413px
const HALF_OVERFLOW = (RENDERED_VIDEO_WIDTH - CANVAS_WIDTH) / 2;

type CameraBeat = { start: number; end: number; panPx: number; zoom: number };

function findNearestSample(samples: FaceSample[], t: number): FaceSample | null {
    if (samples.length === 0) return null;
    let best = samples[0];
    let bestDiff = Math.abs(samples[0].t - t);
    for (const s of samples) {
          const diff = Math.abs(s.t - t);
          if (diff < bestDiff) {
                  best = s;
                  bestDiff = diff;
          }
    }
    return best;
}

// Pretvara STVARNO izmerenu poziciju lica (xFrac, wFrac - udeo sirine
// izvornog kadra) u tacan pomeraj (panPx) i zum potreban da to lice zavrsi
// centrirano u 9:16 kadru.
function computePanAndZoom(sample: FaceSample) {
    const videoX = sample.xFrac * RENDERED_VIDEO_WIDTH;
    let panPx = CANVAS_WIDTH / 2 - videoX + HALF_OVERFLOW;
    // Ne dozvoli da pomeranje otkrije prazninu van ruba videa.
    panPx = Math.max(-HALF_OVERFLOW * 0.92, Math.min(HALF_OVERFLOW * 0.92, panPx));

    const currentFaceWidthPx = Math.max(0.02, sample.wFrac) * RENDERED_VIDEO_WIDTH;
    const desiredFaceWidthPx = 240; // ciljna sirina lica u 1080px-sirokom kadru
    let zoom = desiredFaceWidthPx / currentFaceWidthPx;
    zoom = Math.max(0.95, Math.min(1.55, zoom));
    return { panPx, zoom };
}

function buildCameraBeats(
    words: Word[],
    minBeatSeconds: number,
    facePositions: FaceSample[]
): CameraBeat[] {
    const groups = groupWords(words);
    if (groups.length === 0) return [];

    const rawBeats: { start: number; end: number }[] = [];
    let currentStart: number | null = null;
    let currentEnd = 0;

    for (const g of groups) {
          const gStart = g[0].start;
          const gEnd = g[g.length - 1].end;
          if (currentStart === null) {
                  currentStart = gStart;
                  currentEnd = gEnd;
                  continue;
          }
          if (gEnd - currentStart >= minBeatSeconds) {
                  rawBeats.push({ start: currentStart, end: gEnd });
                  currentStart = null;
          } else {
                  currentEnd = gEnd;
          }
    }
    if (currentStart !== null) {
          rawBeats.push({ start: currentStart, end: Math.max(currentEnd, currentStart + minBeatSeconds) });
    }

    return rawBeats.map((b) => {
          const midT = (b.start + b.end) / 2;
          const sample = findNearestSample(facePositions, midT);
          const { panPx, zoom } = sample ? computePanAndZoom(sample) : { panPx: 0, zoom: 1.1 };
          return { start: b.start, end: b.end, panPx, zoom };
    });
}

function useCameraState(beats: CameraBeat[]) {
    const frame = useCurrentFrame();
    const { fps } = useVideoConfig();
    const t = frame / fps;

    const beat =
          beats.find((b) => t >= b.start && t < b.end) ??
          beats[beats.length - 1] ?? { start: 0, end: 1, panPx: 0, zoom: 1.1 };

    const holdProgress = interpolate(t, [beat.start, beat.end], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });
    // Vrlo blago, kontinuirano "disanje"-zum tokom drzanja kadra (Ken Burns) -
    // osnovni zum vec dolazi od stvarno izmerene velicine lica (beat.zoom).
    const breathe = 1 + holdProgress * 0.04;

    // "Punch" - brz nalet zuma odmah posle reza, pa smirivanje - daje
    // energican, "seckan" osecaj na svakom rezu kamere.
    const timeSinceCut = t - beat.start;
    const punch = interpolate(timeSinceCut, [0, 0.18], [1.1, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });

    // Kratak beo bljesak na svakom rezu - vizuelni "wow" akcenat.
    const flashOpacity = interpolate(
          timeSinceCut,
          [0, 0.05, 0.16],
          [0.28, 0.08, 0],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
        );

    return {
          panPx: beat.panPx,
          scale: beat.zoom * breathe * punch,
          flashOpacity,
    };
}

export const TrailblazersReel: React.FC<Props> = ({
    videoPath,
    bgMusicPath,
    bgMusicVolume,
    words,
    facePositions,
}) => {
    const beats = React.useMemo(
          () => buildCameraBeats(words, 2.6, facePositions ?? []),
          [words, facePositions]
        );
    const { panPx, scale, flashOpacity } = useCameraState(beats);

    return (
          <AbsoluteFill style={{ backgroundColor: "#000" }}>
                  <AbsoluteFill style={{ overflow: "hidden" }}>
                      <Video
                                src={staticFile(videoPath)}
                                style={{
                                    position: "absolute",
                                    top: 0,
                                    left: "50%",
                                    height: CANVAS_HEIGHT,
                                    width: RENDERED_VIDEO_WIDTH,
                                    objectFit: "fill",
                                    transform: `translateX(calc(-50% + ${panPx}px)) scale(${scale})`,
                                    transformOrigin: "center center",
                                }}
                              />
                  </AbsoluteFill>
                  <AbsoluteFill style={{ backgroundColor: "#FFFFFF", opacity: flashOpacity }} />
                  <BackgroundAccents />
                  <Branding />
                  <Captions words={words} />
            {bgMusicPath ? (
                    <Audio src={staticFile(bgMusicPath)} volume={bgMusicVolume} loop />
                  ) : null}
          </AbsoluteFill>
        );
};
