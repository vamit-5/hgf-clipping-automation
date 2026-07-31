import os
import sys
import json
import time
import random
import subprocess
import shutil
import signal
import datetime
import cv2
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ---------------------------------------------------------------------------
# Trailblazers x Ann Miura-Ko - DEO 1 od 2: pravi finalni video (ostaje na
# self-hosted runneru, jer treba OpenCV/Remotion/ffmpeg - lokalni resursi).
# - PROMENA (razdvajanje na 2 posla): OVAJ fajl VISE NE OBJAVLJUJE na
# Instagram. Umesto toga, cuva final_reel.mp4 + podatke za objavu
# (caption, koriscen segment, naziv hook-a) u
# output_clips_schedule/publish_data.json, koje GitHub Actions workflow
# prosledjuje kao "artifact" DRUGOM poslu (trailblazers_schedule_publish.py)
# koji se izvrsava na GitHub-ovom cloud serveru (ne na tvom racunaru).
# Razlog: identican, potpuno ispravan Instagram token je USPESNO radio
# pozvan sa GitHub cloud IP adrese, ali je DOSLEDNO padao sa "session
# invalidated" kad je pozvan sa ovog kucnog racunara - sto ukazuje da Meta
# filtrira/degradira zahteve sa te IP adrese/mreze, ne da je token neispravan.
# - Dnevni brojac i "iskorisceni segmenti" se VISE NE azuriraju ovde - to
# sad radi drugi skript, TEK POSLE potvrdjene uspesne objave (da se ne
# oznaci tema kao "iskoriscena" ako objava nikad nije uspela).
# - Git-baziran katanac se i dalje zauzima/oslobadja OVDE (stiti od
# preklapanja teskih poslova pravljenja videa na istom racuneru).
# ---------------------------------------------------------------------------

SOURCE_PATH = "annmiura_source.mp4"
FFMPEG_TIMEOUT = 900
FFPROBE_TIMEOUT = 60
REMOTION_TIMEOUT = 900

MIN_CLIP_SECONDS = 30
MAX_CLIP_SECONDS = 60
MAX_SINGLE_CLIP_SECONDS = 18
TRANSITION_SECONDS = 0.35

BACKGROUND_AUDIO_FILE_IDS = [
    "1yHsLDQ9yUUe6VtppKUa_MD978Gz7OOHR",
    "1ANHCMAKisUvpxR8KYp0zRnkmKzblj8PN",
]
SOURCE_VIDEO_FILE_ID = "1LUOtWlv4M_Zy4XxTAKcyojCfCerdHla1"
BACKGROUND_AUDIO_VOLUME = 0.45
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

REMOTION_COMPOSITION_ID = "TrailblazersReel"
REMOTION_WORKDIR = "remotion/trailblazers"

DAILY_TARGET = 10

STATE_DIR = "state"
TRANSCRIPT_CACHE_PATH = os.path.join(STATE_DIR, "trailblazers_transcript_cache.json")
USED_SEGMENTS_PATH = os.path.join(STATE_DIR, "trailblazers_used_segments.json")
DAILY_COUNTER_PATH = os.path.join(STATE_DIR, "trailblazers_daily_counter.json")
LOCK_PATH = os.path.join(STATE_DIR, "trailblazers_lock.txt")
LOCK_FRESH_MINUTES = 25  # duze od najsporijeg moguceg pokretanja

OUTPUT_DIR = "output_clips_schedule"
PUBLISH_DATA_PATH = os.path.join(OUTPUT_DIR, "publish_data.json")

# 5 zvanicno odobrenih ideja iz brief-a (CLIPFARM - Ann Miura-Ko x Trailblazers)
APPROVED_IDEAS = [
    "Ideja koja nije imala smisla: Ann otvoreno prica o predlogu/ideji koju "
    "su svi drugi odbili (pass-ovali) jer je delovala pogresno, dok nije "
    "postala ocigledna. Trazi konkretnu pricu/primer.",
    "Opklada koja se vratila 10.000 puta: Ann prica o svojoj ranoj "
    "investiciji i zasto je videla nesto sto skoro niko drugi nije video, "
    "i kakav je bio ogroman povracaj.",
    "'Thunder lizard' osnivaci: Ann prica o tipu osnivaca koji preuzima "
    "celu kategoriju - zabavan, pamtljiv opis tog tipa licnosti.",
    "Kako znas da (proizvod) radi: Ann prica o tacnom trenutku kad "
    "product-market fit prestaje da bude osecaj i postaje merljiva "
    "cinjenica koju ne mozes falsifikovati.",
    "Doktorat, beba, novi fond: licna prica o tome kako je zavrsila "
    "doktorat nedelje posle porodjaja dok je pokretala fond - ljudski, "
    "relatable trenutak.",
]

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


# --------------------------- stanje (state/) --------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def today_str():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def get_daily_count():
    data = load_json(DAILY_COUNTER_PATH, {"date": today_str(), "count": 0})
    if data.get("date") != today_str():
        return 0
    return int(data.get("count", 0))


def get_used_segments():
    return load_json(USED_SEGMENTS_PATH, [])


def write_github_output(key, value):
    """Upisuje vrednost u GITHUB_OUTPUT fajl da bi je drugi posao u istom
    workflow-u mogao da procita (npr. da li uopste treba da pokusa objavu)."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


# --------------------------- git-baziran katanac -----------------------------

def run_git(args, check=True):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=check,
    )


LOCK_HELD = False


def release_lock():
    global LOCK_HELD
    if not LOCK_HELD:
        return
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
        run_git(["add", LOCK_PATH], check=False)
        run_git(["commit", "-m", "Oslobodi trailblazers katanac"], check=False)
        for attempt in range(5):
            run_git(["fetch", "origin"], check=False)
            rebase = run_git(["rebase", "origin/main"], check=False)
            if rebase.returncode != 0:
                run_git(["rebase", "--abort"], check=False)
                time.sleep(3)
                continue
            push = run_git(["push", "origin", "main"], check=False)
            if push.returncode == 0:
                break
            time.sleep(3)
    except Exception as e:
        print(f"[katanac] Upozorenje: nisam uspeo cisto da oslobodim katanac: {e}")
    LOCK_HELD = False


def _signal_handler(signum, frame):
    print(f"[katanac] Primljen signal {signum}, oslobadjam katanac pre izlaska.")
    release_lock()
    sys.exit(1)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def acquire_lock():
    """Vraca True ako je katanac uspesno zauzet, False ako ga neko drugi
    vec drzi (svez, mladji od LOCK_FRESH_MINUTES)."""
    global LOCK_HELD
    os.makedirs(STATE_DIR, exist_ok=True)
    run_git(["fetch", "origin"], check=False)
    run_git(["reset", "--hard", "origin/main"], check=False)

    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                locked_at = datetime.datetime.fromisoformat(f.read().strip())
            age_minutes = (datetime.datetime.utcnow() - locked_at).total_seconds() / 60
            if age_minutes < LOCK_FRESH_MINUTES:
                print(f"[katanac] Neko drugi vec drzi katanac (star {age_minutes:.1f} min). Preskacem.")
                return False
            print(f"[katanac] Stari katanac ({age_minutes:.1f} min) - preuzimam.")
        except (ValueError, OSError):
            pass

    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(datetime.datetime.utcnow().isoformat())

    run_git(["add", LOCK_PATH], check=False)
    commit = run_git(["commit", "-m", "Zauzmi trailblazers katanac"], check=False)
    if commit.returncode != 0:
        print("[katanac] Nista za komit (mozda vec zauzeto) - preskacem.")
        return False

    push = run_git(["push", "origin", "main"], check=False)
    if push.returncode != 0:
        print("[katanac] Push nije uspeo - neko drugi je upravo zauzeo katanac. Preskacem.")
        run_git(["reset", "--hard", "origin/main"], check=False)
        return False

    LOCK_HELD = True
    print("[katanac] Zauzet.")
    return True


# --------------------------- video/audio pipeline ----------------------------

def get_duration_seconds(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=FFPROBE_TIMEOUT,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def extract_audio(source_path, audio_path, duration_seconds):
    target_bitrate = max(24, min(64, int((23 * 8 * 1024) / duration_seconds)))
    print(f"Izdvajam audio pri {target_bitrate}kbps...")
    cmd = ["ffmpeg", "-y", "-i", source_path, "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k", audio_path]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=FFMPEG_TIMEOUT,
    )
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


def get_cached_transcript(source_path, openai_key):
    cache = load_json(TRANSCRIPT_CACHE_PATH, None)
    if cache and cache.get("source") == source_path and cache.get("words"):
        print(f"[kes] Koristim kesiran transkript ({len(cache['words'])} reci) - preskacem Whisper.")
        return cache["words"]

    duration = get_duration_seconds(source_path)
    audio_path = "annmiura_audio_schedule.mp3"
    extract_audio(source_path, audio_path, duration)
    words = transcribe_audio(audio_path, openai_key)
    save_json(TRANSCRIPT_CACHE_PATH, {"source": source_path, "words": words})
    print(f"[kes] Transkript sacuvan u kes ({len(words)} reci).")
    return words


def snap_time_to_words(target, words, key):
    if not words:
        return target
    closest = min(words, key=lambda w: abs(w[key] - target))
    return closest[key]


# --------------------------- Claude hook-detekcija ---------------------------

def find_next_hook(words, api_key, total_duration, approved_ideas, used_segments):
    lines = [f"[{w['start']:.1f}] {w['word']}" for w in words]
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    used_ranges_text = "\n".join(
        f"- {u['hook']}: {u['clips']}" for u in used_segments
    ) or "(jos nista objavljeno)"

    ideas_text = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(approved_ideas))

    prompt = (
        "Ovo je transkript epizode biznis/VC podkasta 'Trailblazers' (gost: Ann Miura-Ko, VC "
        "investitorka - Floodgate), sa vremenskim oznakama u sekundama pre svake reci "
        "(format [12.3] rec).\n\n"
        f"{transcript_text}\n\n"
        "Ovo su ZVANICNO ODOBRENE ideje za klipove iz brief-a klijenta:\n"
        f"{ideas_text}\n\n"
        "Ovo su segmenti (vremenski opsezi) koji su VEC objavljeni - ne smes ih ponovo koristiti, "
        "ni preklapajuce delove transkripta:\n"
        f"{used_ranges_text}\n\n"
        "Zadatak: napravi TACNO JEDAN nov 'supercut' klip (3-5 kratkih pojedinacnih izjava spojenih "
        "jedna za drugom) koji NIJE vec objavljen. Prvo probaj prvu odobrenu ideju sa liste koja jos "
        "nije iskoriscena. Ako su SVE odobrene ideje vec iskoriscene ili nijedna vise nema neiskoriscen "
        "materijal u transkriptu, predlozi NOVU temu u ISTOM duhu kao odobrene: mora biti (a) standalone "
        "advice klip - jedna jasna lekcija od pocetka do kraja, ili (b) hot-take/kontrarian klip - Ann "
        "se suprotstavlja uobicajenom misljenju, ili (c) story klip - ljudski, licni trenutak koji "
        "potkrepljuje lekciju. NIKAD nasumican/nepovezan footage bez prepoznatljivog trenutka.\n\n"
        "Pravila:\n"
        "- Prva izjava (hook) mora biti TRENUTNO jasna kao naslov clanka - gledalac mora u prve 2 "
        "sekunde da zna TACNO o cemu je rec. NE na uvodu ili najavi.\n"
        "- Svaka izjava mora poceti TACNO na pocetku recenice i zavrsiti tacno na kraju - cist rez, "
        "bez 'mrtvog vazduha', prva rec se nikad ne sme cuti isecena.\n"
        f"- Ukupno trajanje: izmedju {MIN_CLIP_SECONDS} i {MAX_CLIP_SECONDS} sekundi.\n"
        "- Koristi TACNE reci iz transkripta - ne parafraziraj i ne izmisljaj.\n\n"
        "AKO STVARNO NEMA vise nijedne teme koja se ne preklapa sa vec objavljenim, odgovori sa "
        "praznim clips nizom i objasni to u reason polju.\n\n"
        "Napravi i JEDINSTVEN Instagram caption (na engleskom, 1-2 recenice). Caption MORA da sadrzi: "
        "(1) eksplicitno pomene 'Ann Miura-Ko' i 'Trailblazers', (2) tacnu recenicu 'Subscribe to "
        "@thetrailblazerspod on YouTube for the full episode', (3) odvojen tag '@trailblazers_pod', "
        "(4) 3-5 relevantnih hashtag-ova (npr #VC #Startups #AnnMiuraKo #Trailblazers #VentureCapital).\n\n"
        "Napravi i kratak 'hook' label (par reci na srpskom, za nase interno pamcenje koje smo teme "
        "vec iskoristili).\n\n"
        "Odgovori ISKLJUCIVO validnim JSON objektom, bez ikakvog dodatnog teksta:\n"
        '{"clips": [{"start": <broj>, "end": <broj>}, ...], '
        '"reason": "<kratko objasnjenje na srpskom>", '
        '"caption": "<Instagram caption na engleskom>", '
        '"hook_label": "<kratak label na srpskom>"}'
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}

    def do_call():
        return requests.post(url, headers=headers, json=payload, timeout=180)

    response = retry_request(do_call, "Claude hook-detekcija (schedule)")
    text = response.json()["content"][0]["text"].strip()
    print(f"[dijagnostika] Sirov Claude odgovor (prvih 1000 karaktera):\n{text[:1000]}")
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        h = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[dijagnostika] Claude odgovor NIJE validan JSON: {e}")
        raise RuntimeError("Claude nije vratio validan JSON.") from e

    raw_clips = h.get("clips", [])
    print(f"[dijagnostika] Claude reason: {h.get('reason', '(nema)')}")

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
        return None

    hard_ceiling = MAX_CLIP_SECONDS + 15
    total = 0.0
    trimmed = []
    for s, e in snapped_clips:
        length = e - s
        if total + length > hard_ceiling and trimmed:
            break
        trimmed.append([s, e])
        total += length

    caption = (h.get("caption", "") or "").strip()
    if "@trailblazers_pod" not in caption:
        caption = (caption + " @trailblazers_pod").strip()

    return {
        "clips": trimmed,
        "reason": h.get("reason", ""),
        "caption": caption,
        "hook_label": h.get("hook_label", "nepoznat hook"),
    }


def build_supercut_with_transitions(source_path, clips, output_path, transition=TRANSITION_SECONDS):
    n = len(clips)
    durations = [round(end - start, 3) for start, end in clips]

    filter_parts = []
    for i, (start, end) in enumerate(clips):
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}];"
        )

    if n == 1:
        filter_parts.append("[v0]copy[vcat];[a0]acopy[acat]")
    else:
        cum = durations[0]
        prev_v = "v0"
        for i in range(1, n):
            offset = max(0.0, cum - transition)
            out_v = "vcat" if i == n - 1 else f"vx{i}"
            filter_parts.append(
                f"[{prev_v}][v{i}]xfade=transition=fade:duration={transition}:offset={offset:.3f}[{out_v}];"
            )
            cum = cum + durations[i] - transition
            prev_v = out_v
        prev_a = "a0"
        for i in range(1, n):
            out_a = "acat" if i == n - 1 else f"ax{i}"
            filter_parts.append(f"[{prev_a}][a{i}]acrossfade=d={transition}[{out_a}];")
            prev_a = out_a

    filter_complex = "".join(filter_parts)
    cmd = [
        "ffmpeg", "-y", "-i", source_path, "-filter_complex", filter_complex,
        "-map", "[vcat]", "-map", "[acat]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-vsync", "cfr",
        "-c:a", "aac", "-b:a", "192k", output_path,
    ]
    print(f"Spajam {n} izjava sa {transition}s tranzicijama...")
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=FFMPEG_TIMEOUT,
    )
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Spajanje sa tranzicijama nije uspelo.")


def get_drive_service():
    from google.oauth2 import service_account
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    import httplib2

    creds_info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    credentials = service_account.Credentials.from_service_account_info(creds_info, scopes=DRIVE_SCOPES)
    http = httplib2.Http(timeout=180)
    authorized_http = AuthorizedHttp(credentials, http=http)
    return build("drive", "v3", http=authorized_http, cache_discovery=False)


def download_drive_file(service, file_id, destination):
    from googleapiclient.http import MediaIoBaseDownload

    if os.path.exists(destination):
        return
    tmp = destination + ".partial"
    request = service.files().get_media(fileId=file_id)
    with open(tmp, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk(num_retries=5)
    os.rename(tmp, destination)


def analyze_face_positions(video_path, sample_interval=0.4):
    frontal_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    profile_path = cv2.data.haarcascades + "haarcascade_profileface.xml"
    frontal_cascade = cv2.CascadeClassifier(frontal_path)
    profile_cascade = cv2.CascadeClassifier(profile_path)
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = (total_frames / fps) if fps else 0

    if frame_w == 0 or frame_h == 0 or duration <= 0:
        cap.release()
        return []

    def detect_best_face(gray):
        faces = frontal_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        candidates = list(faces)
        profile_faces = profile_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        candidates.extend(profile_faces)
        flipped = cv2.flip(gray, 1)
        flipped_profile = profile_cascade.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        for (fx, fy, fw, fh) in flipped_profile:
            candidates.append((gray.shape[1] - fx - fw, fy, fw, fh))
        if not candidates:
            return None
        return max(candidates, key=lambda f: f[2] * f[3])

    samples = []
    last_x_frac, last_w_frac = 0.5, 0.35
    miss_streak = 0
    MAX_FROZEN_MISSES = 3
    SAFE_WIDE_W_FRAC = 0.55
    t = 0.0
    while t < duration:
        frame_idx = min(total_frames - 1, int(t * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            miss_streak += 1
            x_frac, w_frac = (0.5, SAFE_WIDE_W_FRAC) if miss_streak > MAX_FROZEN_MISSES else (last_x_frac, last_w_frac)
            samples.append({"t": round(t, 2), "xFrac": x_frac, "wFrac": w_frac})
            t += sample_interval
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        face = detect_best_face(gray)

        if face is None:
            miss_streak += 1
            x_frac, w_frac = (0.5, SAFE_WIDE_W_FRAC) if miss_streak > MAX_FROZEN_MISSES else (last_x_frac, last_w_frac)
        else:
            fx, fy, fw, fh = face
            x_frac = (fx + fw / 2) / frame_w
            w_frac = fw / frame_w
            last_x_frac, last_w_frac = x_frac, w_frac
            miss_streak = 0

        samples.append({"t": round(t, 2), "xFrac": round(x_frac, 4), "wFrac": round(w_frac, 4)})
        t += sample_interval

    cap.release()
    print(f"[dijagnostika] Detekcija lica: {len(samples)} uzoraka analizirano.")
    return samples


def render_with_remotion(video_path, words, duration_seconds, output_path, face_positions, background_audio_path=None):
    remotion_public_dir = os.path.join(REMOTION_WORKDIR, "public")
    os.makedirs(remotion_public_dir, exist_ok=True)
    video_dest = os.path.join(remotion_public_dir, "input_video.mp4")
    shutil.copyfile(video_path, video_dest)
    video_public_path = "input_video.mp4"

    music_public_path = ""
    if background_audio_path:
        music_dest = os.path.join(remotion_public_dir, "bg_music.mp3")
        shutil.copyfile(background_audio_path, music_dest)
        music_public_path = "bg_music.mp3"

    props = {
        "videoPath": video_public_path,
        "bgMusicPath": music_public_path,
        "bgMusicVolume": BACKGROUND_AUDIO_VOLUME,
        "durationInSeconds": duration_seconds,
        "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in words],
        "facePositions": face_positions,
    }
    props_path = os.path.join(OUTPUT_DIR, "remotion_props.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f)

    npx_cmd = shutil.which("npx") or "npx"
    cmd = [
        npx_cmd, "remotion", "render",
        REMOTION_COMPOSITION_ID, os.path.abspath(output_path),
        f"--props={os.path.abspath(props_path)}",
        "--log=verbose",
    ]

    RENDER_ATTEMPTS = 3
    last_error_output = ""
    for attempt in range(1, RENDER_ATTEMPTS + 1):
        print(f"Renderujem finalni video kroz Remotion (koristim: {npx_cmd}), pokusaj {attempt}/{RENDER_ATTEMPTS}...")
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=REMOTION_TIMEOUT, cwd=REMOTION_WORKDIR,
        )
        if result.returncode == 0:
            print(f"Remotion render zavrsen: {output_path}")
            return
        stdout_part = (result.stdout or "")[-2000:]
        stderr_part = (result.stderr or "")[-2000:]
        last_error_output = stdout_part + "\n" + stderr_part
        print(f"[Remotion pokusaj {attempt}] Nije uspeo:")
        print(last_error_output)
        if attempt < RENDER_ATTEMPTS:
            wait_seconds = 15 * attempt
            print(f"Cekam {wait_seconds}s pre ponovnog pokusaja...")
            time.sleep(wait_seconds)

    raise RuntimeError(f"Remotion render nije uspeo nakon {RENDER_ATTEMPTS} pokusaja. Poslednja greska:\n{last_error_output}")


# --------------------------- glavni tok ---------------------------------------

def main():
    daily_count = get_daily_count()
    if daily_count >= DAILY_TARGET:
        print(f"[cilj] Danas je vec objavljeno {daily_count}/{DAILY_TARGET}. Zavrseno za danas, izlazim.")
        write_github_output("should_publish", "false")
        return

    if not acquire_lock():
        write_github_output("should_publish", "false")
        return

    try:
        openai_key = os.environ["OPENAI_API_KEY"]
        anthropic_key = os.environ["ANTHROPIC_API_KEY"]

        if not os.path.exists(SOURCE_PATH):
            print(f"{SOURCE_PATH} ne postoji lokalno - preuzimam sa Google Drive-a...")
            if not os.environ.get("GDRIVE_CREDENTIALS_JSON"):
                raise RuntimeError(
                    "GDRIVE_CREDENTIALS_JSON nije podesen - ne mogu da preuzmem izvorni video."
                )
            drive_service = get_drive_service()
            download_drive_file(drive_service, SOURCE_VIDEO_FILE_ID, SOURCE_PATH)
            print(f"Izvorni video preuzet sa Google Drive-a: {SOURCE_PATH}")

        duration = get_duration_seconds(SOURCE_PATH)
        words = get_cached_transcript(SOURCE_PATH, openai_key)

        used_segments = get_used_segments()
        hook = find_next_hook(words, anthropic_key, duration, APPROVED_IDEAS, used_segments)

        if hook is None:
            print("[hook] Nijedna nova, neponovljena tema nije pronadjena za sada. Preskacem ovaj run.")
            write_github_output("should_publish", "false")
            return

        clips = hook["clips"]
        print(f"Izabran segment: {len(clips)} izjava, {clips} - {hook['hook_label']}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        supercut_path = os.path.join(OUTPUT_DIR, "supercut.mp4")
        final_path = os.path.join(OUTPUT_DIR, "final_reel.mp4")

        build_supercut_with_transitions(SOURCE_PATH, clips, supercut_path)
        sc_duration = get_duration_seconds(supercut_path)
        print(f"Trajanje supercuta: {sc_duration:.1f}s")

        sc_audio = os.path.join(OUTPUT_DIR, "supercut_audio.mp3")
        extract_audio(supercut_path, sc_audio, sc_duration)
        sc_words = transcribe_audio(sc_audio, openai_key)

        face_positions = analyze_face_positions(supercut_path)

        chosen_background_audio = None
        if os.environ.get("GDRIVE_CREDENTIALS_JSON"):
            try:
                service = get_drive_service()
                paths = []
                for i, fid in enumerate(BACKGROUND_AUDIO_FILE_IDS):
                    p = f"bg_audio_{i}.mp3"
                    download_drive_file(service, fid, p)
                    paths.append(p)
                chosen_background_audio = random.choice(paths)
            except Exception as e:
                print(f"Nisam uspeo da preuzmem pozadinsku muziku (nastavljam bez nje): {e}")

        render_with_remotion(
            supercut_path, sc_words, sc_duration, final_path,
            face_positions, background_audio_path=chosen_background_audio,
        )

        save_json(PUBLISH_DATA_PATH, {
            "caption": hook["caption"],
            "clips": clips,
            "hook_label": hook["hook_label"],
        })

        print(f"[build] Video i podaci za objavu spremni: {final_path}")
        write_github_output("should_publish", "true")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
