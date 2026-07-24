import os
import sys
import json
import time
import subprocess
import requests

sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Trailblazers x Ann Miura-Ko - JEDNOKRATNI TEST skript (dry-run, NE objavljuje
# na Instagram). Cilj: skini izvorni video sa WeTransfer linka (bez Google
# Drive-a), transkribuj, nadji NAJJACE (sokantne/kontroverzne) izjave pomocu
# Claude-a, napravi supercut sa titlovima u istom vizuelnom stilu kao HGF
# (blur pozadina, watermark opciono), i sacuvaj kao workflow artifact.
# ---------------------------------------------------------------------------

WETRANSFER_SHORT_URL = "https://we.tl/t-CNSBnb2WgM3qgNGe"
SOURCE_PATH = "annmiura_source.mp4"
FFMPEG_TIMEOUT = 900
FFPROBE_TIMEOUT = 60

MIN_CLIP_SECONDS = 25
MAX_CLIP_SECONDS = 50
MAX_SINGLE_CLIP_SECONDS = 18
N_HOOKS = 5

RETRY_ATTEMPTS = 5
RETRY_DELAYS = [5, 10, 20, 40]


def retry_request(func, description):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = func()
            if response.status_code < 400:
                return response
            if 400 <= response.status_code < 500:
                print(f"[{description}] TRAJNA GRESKA {response.status_code}: {response.text[:500]}")
                raise RuntimeError(f"{description} nije uspeo (trajna greska {response.status_code}).")
            print(f"[{description}] Privremena greska {response.status_code}, pokusaj {attempt}/{RETRY_ATTEMPTS}")
            last_error = RuntimeError(f"{description}: {response.status_code} {response.text[:500]}")
        except requests.RequestException as e:
            print(f"[{description}] Mrezna greska, pokusaj {attempt}/{RETRY_ATTEMPTS}: {e}")
            last_error = e
        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_DELAYS[attempt - 1]
            print(f"Cekam {delay}s pre sledeceg pokusaja...")
            time.sleep(delay)
    raise RuntimeError(f"{description} nije uspeo nakon {RETRY_ATTEMPTS} pokusaja.") from last_error


def download_from_wetransfer(short_url, destination):
    """WeTransfer nema zvanicni javni API, ali /downloads/ stranica embeduje
    transfer_id i security_hash u URL-u posle redirekta sa kratkog linka.
    Njihov interni v4 API (koriscen od strane same wetransfer.com stranice
    kad korisnik klikne 'Download') prima te podatke i vraca potpisani
    direktni CDN link."""
    print(f"Pratim WeTransfer redirekt sa {short_url}...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})

    resp = session.get(short_url, allow_redirects=True, timeout=60)
    final_url = resp.url
    print(f"Zavrsni URL: {final_url}")

    # Ocekivani oblik: https://wetransfer.com/downloads/{transfer_id}/{recipient_id}?...
    parts = final_url.split("wetransfer.com/downloads/")[-1].split("?")[0].strip("/").split("/")
    if len(parts) < 2:
        raise RuntimeError(f"Nisam uspeo da izvucem transfer_id/security_hash iz URL-a: {final_url}")
    transfer_id, security_hash = parts[0], parts[1]
    print(f"transfer_id={transfer_id} security_hash={security_hash}")

    api_url = f"https://wetransfer.com/api/v4/transfers/{transfer_id}/download"
    payload = {"security_hash": security_hash, "intent": "entire_transfer"}

    def do_call():
        return session.post(api_url, json=payload, timeout=60)

    api_resp = retry_request(do_call, "WeTransfer API poziv")
    direct_link = api_resp.json().get("direct_link")
    if not direct_link:
        raise RuntimeError(f"WeTransfer API nije vratio direct_link: {api_resp.text[:500]}")
    print("Dobijen direktan CDN link, preuzimam fajl...")

    with session.get(direct_link, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        last_pct = -1
        with open(destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct and pct % 10 == 0:
                            print(f"Preuzeto: {pct}%")
                            last_pct = pct
    size_mb = os.path.getsize(destination) / (1024 * 1024)
    print(f"Preuzimanje zavrseno: {destination} (~{size_mb:.1f} MB)")


def get_duration_seconds(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=FFPROBE_TIMEOUT)
    return float(json.loads(result.stdout)["format"]["duration"])


def extract_audio(source_path, audio_path, duration_seconds):
    target_bitrate = max(24, min(64, int((23 * 8 * 1024) / duration_seconds)))
    print(f"Izdvajam audio pri {target_bitrate}kbps...")
    cmd = ["ffmpeg", "-y", "-i", source_path, "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Izdvajanje audia nije uspelo.")


def transcribe_audio(audio_path, api_key):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}

    def do_transcribe():
        with open(audio_path, "rb") as f:
            files = {"file": f}
            data = {"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "word"}
            return requests.post(url, headers=headers, files=files, data=data, timeout=900)

    response = retry_request(do_transcribe, "Whisper transkripcija")
    return response.json().get("words", [])


def snap_time_to_words(target, words, key):
    if not words:
        return target
    closest = min(words, key=lambda w: abs(w[key] - target))
    return closest[key]


def find_hook_segments(words, api_key, total_duration, n_hooks=N_HOOKS):
    lines = [f"[{w['start']:.1f}] {w['word']}" for w in words]
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    prompt = (
        "Ovo je transkript epizode biznis/VC podkasta 'Trailblazers' (gost: Ann Miura-Ko, "
        "poznata VC investitorka - Floodgate, rana investicija u Lyft), sa vremenskim oznakama "
        "u sekundama pre svake reci (format [12.3] rec).\n\n"
        f"{transcript_text}\n\n"
        f"Napravi {n_hooks} RAZLICITIH kratkih 'supercut' klipova za Instagram Reels/TikTok/YouTube "
        "Shorts. Svaki supercut je SASTAVLJEN OD VISE KRATKIH pojedinacnih izjava spojenih jedna za "
        "drugom (NE jedan neprekinuti isecak). Cilj: SAMO najsokantnije, najkontroverznije, "
        "najdramaticnije, najkorisnije pojedinacne izjave o VC-u/investiranju/preduzetnistvu - bez "
        "uvoda ili dosadnog konteksta.\n\n"
        "PRVA izjava (hook) mora biti TRENUTNO jasna kao naslov clanka - npr. o njenoj ranoj Lyft "
        "investiciji, o 'ideji koja nije imala smisla', o tome kakve osnivace trazi ('thunder "
        "lizard' founders), ili neka druga kontroverzna/iznenadjujuca izjava o VC svetu. Gledalac "
        "mora u prve 2 sekunde da zna TACNO o cemu je rec.\n\n"
        "Pravila:\n"
        "- Svaka pojedinacna izjava treba da traje ~4-15 sekundi i MORA poceti TACNO na pocetku te "
        "recenice/izjave (ne usred reci) i zavrsiti tacno na kraju te izjave.\n"
        "- Kombinuj 3-6 izjava u JEDAN supercut klip.\n"
        f"- Ukupno trajanje jednog supercut klipa: {MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS}s.\n"
        "- Izjave unutar klipa ne moraju biti hronoloski uzastopne u originalu.\n"
        "- Razliciti supercut klipovi ne smeju deliti iste izjave.\n"
        f"Video traje ukupno {total_duration:.0f}s.\n\n"
        "Za svaki klip napravi JEDINSTVEN caption (engleski, 1-2 recenice) koji se konkretno odnosi "
        "na TAJ sadrzaj. Caption MORA sadrzati: 'Subscribe to @thetrailblazerspod on YouTube for "
        "the full episode', i 3-5 relevantnih hashtag-ova (npr #VC #Startups #AnnMiuraKo "
        "#Trailblazers #VentureCapital). Ne ponavljaj isti caption.\n\n"
        "Odgovori ISKLJUCIVO validnim JSON nizom, bez ikakvog dodatnog teksta:\n"
        '[{"clips": [{"start": <broj>, "end": <broj>}, ...], '
        '"reason": "<kratko objasnjenje na srpskom>", '
        '"caption": "<Instagram caption na engleskom>"}, ...]'
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": "claude-haiku-4-5-20251001", "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]}

    def do_call():
        return requests.post(url, headers=headers, json=payload, timeout=180)

    response = retry_request(do_call, "Claude hook-detekcija")
    text = response.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    raw_hooks = json.loads(text)
    cleaned = []
    for h in raw_hooks:
        raw_clips = h.get("clips", [])
        snapped_clips = []
        for c in raw_clips:
            try:
                start = max(0.0, float(c["start"]))
                end = min(float(c["end"]), total_duration)
            except (KeyError, TypeError, ValueError):
                continue
            if end - start < 1.0:
                continue
            end = min(end, start + MAX_SINGLE_CLIP_SECONDS)
            start = max(0.0, snap_time_to_words(start, words, "start") - 0.15)
            end = min(total_duration, snap_time_to_words(end, words, "end") + 0.15)
            if end - start >= 1.0:
                snapped_clips.append([round(start, 2), round(end, 2)])
        if not snapped_clips:
            continue
        hard_ceiling = MAX_CLIP_SECONDS + 15
        total = 0.0
        trimmed = []
        for s, e in snapped_clips:
            length = e - s
            if total + length > hard_ceiling and trimmed:
                break
            trimmed.append([s, e])
            total += length
        cleaned.append({"clips": trimmed, "reason": h.get("reason", ""), "caption": h.get("caption", "")})

    print(f"Pronadjeno {len(cleaned)} supercut kombinacija.")
    return cleaned


def group_words_into_captions(words, max_words_per_group=4, max_gap=0.6):
    groups = []
    current = []
    for w in words:
        if current and (w["start"] - current[-1]["end"] > max_gap or len(current) >= max_words_per_group):
            groups.append(current)
            current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def format_ass_time(seconds):
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:01d}:{m:02d}:{s:05.2f}"


def build_captions_file(words, clip_start, clip_end, path):
    clip_words = [
        {"word": w["word"], "start": w["start"] - clip_start, "end": w["end"] - clip_start}
        for w in words if clip_start <= w["start"] < clip_end
    ]
    groups = group_words_into_captions(clip_words)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Caption,Liberation Sans,74,&H00FFFFFF,&H00000000,&H00000000,1,0,1,6,0,2,60,60,750

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for group in groups:
        start = group[0]["start"]
        end = group[-1]["end"]
        text = " ".join(w["word"] for w in group).upper()
        lines.append(f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Caption,,0,0,0,,{text}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def build_supercut(source_path, clips, output_path):
    filter_parts = []
    concat_inputs = []
    for i, (start, end) in enumerate(clips):
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}];"
        )
        concat_inputs.append(f"[v{i}][a{i}]")
    n = len(clips)
    filter_parts.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[vcat][acat]")
    filter_complex = "".join(filter_parts)
    cmd = [
        "ffmpeg", "-y", "-i", source_path, "-filter_complex", filter_complex,
        "-map", "[vcat]", "-map", "[acat]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", output_path,
    ]
    print(f"Spajam {n} kratkih izjava u jedan supercut...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Spajanje izjava nije uspelo.")


def finalize_clip(input_path, captions_path, output_path):
    filter_complex = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bgblur];"
        "[fg]scale=1080:-2:force_original_aspect_ratio=decrease[fgscaled];"
        "[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2[framed];"
        f"[framed]ass={captions_path}[outv]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path, "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", output_path,
    ]
    print("Zavrsna obrada (9:16 blur pozadina + titlovi)...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Zavrsna obrada nije uspela.")


def main():
    openai_key = os.environ["OPENAI_API_KEY"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]

    if not os.path.exists(SOURCE_PATH):
        download_from_wetransfer(WETRANSFER_SHORT_URL, SOURCE_PATH)
    else:
        print(f"{SOURCE_PATH} vec postoji, preskacem preuzimanje.")

    duration = get_duration_seconds(SOURCE_PATH)
    print(f"Trajanje izvornog videa: {duration:.1f}s")

    audio_path = "annmiura_audio.mp3"
    extract_audio(SOURCE_PATH, audio_path, duration)

    words = transcribe_audio(audio_path, openai_key)
    print(f"Transkripcija: {len(words)} reci.")

    with open("transcript_debug.json", "w") as f:
        json.dump(words, f, indent=2)

    hooks = find_hook_segments(words, anthropic_key, duration)
    with open("hooks_debug.json", "w") as f:
        json.dump(hooks, f, indent=2)

    if not hooks:
        raise RuntimeError("Nijedan hook nije pronadjen.")

    # Za ovaj test, napravi klip za SVAKI pronadjeni hook (obicno N_HOOKS komada),
    # da korisnik moze da izabere najbolji.
    os.makedirs("output_clips", exist_ok=True)
    for idx, hook in enumerate(hooks):
        clips = hook["clips"]
        supercut_path = f"output_clips/supercut_{idx}.mp4"
        final_path = f"output_clips/final_{idx}.mp4"
        captions_path = f"output_clips/captions_{idx}.ass"

        build_supercut(SOURCE_PATH, clips, supercut_path)
        sc_duration = get_duration_seconds(supercut_path)
        sc_audio = f"output_clips/supercut_{idx}_audio.mp3"
        extract_audio(supercut_path, sc_audio, sc_duration)
        sc_words = transcribe_audio(sc_audio, openai_key)
        build_captions_file(sc_words, 0.0, sc_duration + 1.0, captions_path)
        finalize_clip(supercut_path, captions_path, final_path)

        with open(f"output_clips/info_{idx}.txt", "w") as f:
            f.write(f"Reason: {hook['reason']}\n\nCaption: {hook['caption']}\n\nClips (sec): {clips}\n")

        print(f"Klip {idx} gotov: {final_path}")

    print("SVI TEST KLIPOVI GOTOVI. Nista nije objavljeno na Instagram (dry-run).")


if __name__ == "__main__":
    main()
