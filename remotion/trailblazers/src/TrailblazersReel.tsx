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

export const trailblazersReelSchema = z.object({
    videoPath: z.string(),
    bgMusicPath: z.string().optional(),
    bgMusicVolume: z.number().default(0.45),
    durationInSeconds: z.number(),
    words: z.array(wordSchema),
});

type Props = z.infer<typeof trailblazersReelSchema>;

// Izvorni podkast je snimljen u 16:9, dva govornika sede levo/desno od
// okruglog stola. Mi secemo u 9:16. VAZNO: "objectFit: cover" bi automatski
// isekao video na usku CENTRALNU traku PRE nego sto nas pan/zoom uopste
// stigne da se primeni - to je bio uzrok "prazne sredine" problema. Zato
// ovde RUCNO racunamo pravu sirinu videa (skalirano da popuni visinu
// kadra) i sami pomeramo/zumiramo TAJ neisecen video, cime stvarno mozemo
// da dovedemo levog ili desnog govornika u centar kadra.
const CANVAS_WIDTH = 1080;
const CANVAS_HEIGHT = 1920;
const SOURCE_ASPECT = 16 / 9;
const RENDERED_VIDEO_WIDTH = CANVAS_HEIGHT * SOURCE_ASPECT; // ~3413px

// Kamera "beat"-ovi: diskretni rezovi (ne spor kontinuiran pan) sinhronizovani
// sa promenama titlova, sa pretezno LEVIM fokusom (fallback bez prepoznavanja
// govornika - govornik u standalone_advice klipovima je najcesce levo),
// povremenim kratkim "sirim" kadrom za varijaciju, i retko desnim.
type CameraBeat = { start: number; end: number; panPx: number };

function seededRandom(seed: number) {
    let state = seed;
    return () => {
          state = (state * 9301 + 49297) % 233280;
          return state / 233280;
    };
}

// panPx pomera (neisecen, sirok) video horizontalno: POZITIVNA vrednost
// pomera video UDESNO sto otkriva LEVI deo izvornog kadra (levi govornik),
// NEGATIVNA otkriva desni deo. 0 = centar/oba u kadru (kratko, retko).
const PAN_LEFT = 950;
const PAN_RIGHT = -850;
const PAN_CENTER = 0;

function buildCameraBeats(words: Word[], minBeatSeconds: number): CameraBeat[] {
    const groups = groupWords(words);
    if (groups.length === 0) return [];

    const rand = seededRandom(17);
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
          const r = rand();
          let panPx: number;
          if (r < 0.6) panPx = PAN_LEFT;
          else if (r < 0.82) panPx = PAN_CENTER;
          else panPx = PAN_RIGHT;
          return { start: b.start, end: b.end, panPx };
    });
}

function useCameraState(beats: CameraBeat[]) {
    const frame = useCurrentFrame();
    const { fps } = useVideoConfig();
    const t = frame / fps;

    const beat =
          beats.find((b) => t >= b.start && t < b.end) ??
          beats[beats.length - 1] ?? { start: 0, end: 1, panPx: PAN_LEFT };

    const beatDuration = Math.max(0.5, beat.end - beat.start);
    const holdProgress = interpolate(t, [beat.start, beat.end], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });
    // Spor, kontinuiran "disanje"-zum tokom celog drzanja kadra (Ken Burns).
    const holdZoom = 1.02 + holdProgress * 0.08;

    // "Punch" - brz nalet zuma odmah posle reza, pa smirivanje - daje
    // energican, "seckan" osecaj na svakom rezu kamere.
    const timeSinceCut = t - beat.start;
    const punch = interpolate(timeSinceCut, [0, 0.18], [1.14, 1], {
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
          scale: holdZoom * punch,
          flashOpacity,
          beatDuration,
    };
}

export const TrailblazersReel: React.FC<Props> = ({
    videoPath,
    bgMusicPath,
    bgMusicVolume,
    words,
}) => {
    const beats = React.useMemo(() => buildCameraBeats(words, 2.6), [words]);
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
