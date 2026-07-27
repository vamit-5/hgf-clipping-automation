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

// PROMENA: pre je "beat" (koliko dugo kadar stoji pre sledece tranzicije)
// bio odredjen isivo trajanjem govora (svakih ~2.6s bez obzira sta se desava
// na slici) - zato su tranzicije delovale nasumicno, ne uklopljene sa
// stvarnim sekom kamere u izvornom videu. Sada se novi "beat" pravi SAMO
// kada se STVARNO izmerena pozicija lica (xFrac) znacajno promeni izmedju
// uzoraka - to je trenutak kad se kamera u izvoru stvarno posekla na drugi
// kadar. Da se ne bi "beat" napravio zbog trenutnog suma u detekciji lica
// (jedan promasen frame), nova pozicija mora da se "potvrdi" tako sto
// ostane priblizno ista u sledecih par uzoraka pre nego sto se racuna kao
// prava promena kamere.
const CUT_THRESHOLD = 0.12; // xFrac promena koja znaci "kamera se posekla"
const MIN_BEAT_SECONDS = 0.5; // ne pravi novi beat prebrzo posle prethodnog
const CONFIRM_SAMPLES = 2; // nova pozicija mora da se zadrzi ovoliko uzoraka

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

// Beat-ovi se sada prave iskljucivo iz STVARNIH promena kamere (izmerenih
// preko pozicije lica), ne iz trajanja govora. words/groupWords se i dalje
// koriste samo za same titlove (Captions komponenta), ne za tempo kadra.
function buildCameraBeats(
    facePositions: FaceSample[],
    totalDuration: number
): CameraBeat[] {
    if (facePositions.length === 0) {
          return [{ start: 0, end: totalDuration, panPx: 0, zoom: 1.1 }];
    }

    const sorted = [...facePositions].sort((a, b) => a.t - b.t);
    const rawBeats: { start: number; samples: FaceSample[] }[] = [
          { start: sorted[0].t, samples: [sorted[0]] },
    ];

    for (let i = 1; i < sorted.length; i++) {
          const prev = sorted[i - 1];
          const cur = sorted[i];
          const activeBeat = rawBeats[rawBeats.length - 1];
          const delta = Math.abs(cur.xFrac - prev.xFrac);
          const longEnoughSinceLastCut = cur.t - activeBeat.start >= MIN_BEAT_SECONDS;

          let isRealCut = false;
          if (delta > CUT_THRESHOLD && longEnoughSinceLastCut) {
                  // Potvrdi da nova pozicija nije samo trenutni sum - proveri da
                  // li ostaje priblizno ista u sledecih par uzoraka.
                  let holds = true;
                  for (let k = i; k < Math.min(sorted.length, i + CONFIRM_SAMPLES); k++) {
                          if (Math.abs(sorted[k].xFrac - cur.xFrac) > CUT_THRESHOLD) {
                                  holds = false;
                                  break;
                          }
                  }
                  isRealCut = holds;
          }

          if (isRealCut) {
                  rawBeats.push({ start: cur.t, samples: [cur] });
          } else {
                  activeBeat.samples.push(cur);
          }
    }

    return rawBeats.map((b, idx) => {
          const end = idx < rawBeats.length - 1 ? rawBeats[idx + 1].start : totalDuration;
          // Srednji uzorak (medijana po vremenu) unutar beat-a, radi
          // stabilnijeg kadriranja umesto oslanjanja na jedan frame.
          const midSample = b.samples[Math.floor(b.samples.length / 2)];
          const { panPx, zoom } = computePanAndZoom(midSample);
          return { start: b.start, end, panPx, zoom };
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
    // energican, "seckan" osecaj, ali sada tacno na pravom rezu kamere.
    const timeSinceCut = t - beat.start;
    const punch = interpolate(timeSinceCut, [0, 0.18], [1.1, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
    });

    // Kratak beo bljesak - sada se javlja SAMO na stvarnom rezu kamere.
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
    durationInSeconds,
    words,
    facePositions,
}) => {
    const beats = React.useMemo(
          () => buildCameraBeats(facePositions ?? [], durationInSeconds),
          [facePositions, durationInSeconds]
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
