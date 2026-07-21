import os
import json
import time
import random
import datetime
import subprocess
import requests
import cv2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
WATERMARK_FILE_ID = "1a3FqXNdhtW-QdFq_ww7G_bwdIAUg-7fh"
# Google Drive FILE ID-jevi pozadinske (dramaticne) muzike, bez copyright-a.
# Za svaki objavljeni klip se nasumicno bira JEDNA od ovih numera (radi
# raznovrsnosti). Prazna lista = pozadinska muzika se preskace, nista se ne kvari.
BACKGROUND_AUDIO_FILE_IDS = [
    "1yHsLDQ9yUUe6VtppKUa_MD978Gz7OOHR",
    "1ANHCMAKisUvpxR8KYp0zRnkmKzblj8PN",
]
BACKGROUND_AUDIO_VOLUME = 0.15  # 15% jacine u odnosu na govor - cujno, ali ne prekriva govor
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

STATE_DIR = "state"
USED_SEGMENTS_PATH = f"{STATE_DIR}/used_segments.json"
DAILY_COUNTER_PATH = f"{STATE_DIR}/daily_counter.json"
HOOKS_CACHE_PATH = f"{STATE_DIR}/hooks_cache.json"
LOCK_PATH = f"{STATE_DIR}/lock.txt"

WATERMARK_PATH = "watermark.png"
OUTPUT_PATH = "clip_output.mp4"

MIN_CLIP_SECONDS = 32
MAX_CLIP_SECONDS = 45
HOOKS_PER_FILE = 8
DAILY_TARGET = 10
ALLOWED_UTC_HOURS = set(range(12, 23))  # 12:00 - 22:59 UTC
LOCK_FRESHNESS_MINUTES = 25

CAPTION_TEXT = (
    "@hgf Real talk you need to hear today. Follow @hgf and listen to the "
    "full episode — link in bio. #HotGirlFinance #MoneyTips #FinanceTok"
)

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


# ---------- git helpers ----------

def git_run(cmd):
    return subprocess.run(["git"] + cmd, capture_output=True, text=True)


def git_pull_latest():
    git_run(["fetch", "origin"])
    git_run(["reset", "--hard", "origin/HEAD"])


def try_claim_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    git_pull_latest()
    now = datetime.datetime.utcnow()
    if os.path.exists(LOCK_PATH):
        with open(LOCK_PATH) as f:
            try:
                last_claim = datetime.datetime.fromisoformat(f.read().strip())
                age_minutes = (now - last_claim).total_seconds() / 60
                if age_minutes < LOCK_FRESHNESS_MINUTES:
                    print(f"Katanac je svez ({age_minutes:.1f} min) -> povlacim se, neko drugi vec radi.")
                    return False
            except ValueError:
                pass
    with open(LOCK_PATH, "w") as f:
        f.write(now.isoformat())
    git_run(["add", LOCK_PATH])
    git_run(["commit", "-m", "chore: claim posting lock"])
    push_result = git_run(["push", "origin", "HEAD"])
    if push_result.returncode == 0:
        print("Katanac uspesno zauzet.")
        return True
    print("Push za katanac nije uspeo (neko je bio brzi) -> povlacim se.")
    return False


def commit_and_push_state(message):
    git_run(["add", STATE_DIR])
    commit_result = git_run(["commit", "-m", message])
    if commit_result.returncode != 0 and "nothing to commit" in (commit_result.stdout + commit_result.stderr):
        print("Nema izmena za komitovanje.")
        return True
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        git_run(["fetch", "origin"])
        rebase_result = git_run(["rebase", "origin/HEAD"])
        if rebase_result.returncode != 0:
            print(f"[pokusaj {attempt}] rebase neuspesan, pokusavam ponovo...")
            git_run(["rebase", "--abort"])
            time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
            continue
        push_result = git_run(["push", "origin", "HEAD"])
        if push_result.returncode == 0:
            print("Stanje uspesno sacuvano u repozitorijumu.")
            return True
        print(f"[pokusaj {attempt}] push neuspesan, pokusavam ponovo...")
        time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
    print("UPOZORENJE: cuvanje stanja nije uspelo nakon svih pokusaja.")
    return False


# ---------- state files ----------

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_today_key():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


# ---------- Google Drive ----------

def get_drive_service():
    creds_info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    credentials = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=credentials)


def list_video_files(service, folder_id):
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, size, mimeType)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    videos = [f for f in files if f.get("mimeType", "").startswith("video/")]
    seen_names = set()
    unique_videos = []
    for f in videos:
        if f["name"] not in seen_names:
            seen_names.add(f["name"])
            unique_videos.append(f)
    return unique_videos


def download_by_id(service, file_id, destination):
    if os.path.exists(destination):
        print(f"{destination} vec postoji, preskacem preuzimanje.")
        return
    tmp_destination = destination + ".partial"
    request = service.files().get_media(fileId=file_id)
    with open(tmp_destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            # num_retries>0 makes the library itself retry transient network/5xx
            # errors on this chunk with exponential backoff, instead of failing
            # the whole multi-GB download over one dropped connection.
            status, done = downloader.next_chunk(num_retries=5)
            if status:
                print(f"Preuzeto: {int(status.progress() * 100)}%")
    os.rename(tmp_destination, destination)


def get_duration_seconds(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


# ---------- automatska detekcija pozicije lica (za precizno secenje 9:16) ----------

DEFAULT_CROP_LEFT_X = 313
DEFAULT_CROP_RIGHT_X = 2019
FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


def compute_half_crop_offset(face_center_x, half_start, half_width, crop_width, scale_factor):
    """Racuna x-poziciju secenja (u skaliranom 3413px prostoru) tako da bude
    centrirana na detektovano lice, ali da ostane unutar granica te polovine."""
    scaled_face_center = face_center_x * scale_factor
    scaled_half_start = half_start * scale_factor
    scaled_half_end = (half_start + half_width) * scale_factor
    crop_x = scaled_face_center - crop_width / 2
    crop_x = max(scaled_half_start, min(crop_x, scaled_half_end - crop_width))
    return round(crop_x)


def detect_crop_offsets(video_path, duration_seconds, start_seconds=None, end_seconds=None):
    """Uzima nekoliko frejmova IZ TACNO ONOG DELA snimka koji ce se koristiti za
    klip (start_seconds -> end_seconds) i detektuje gde se tacno nalazi lice u
    levoj i desnoj polovini kadra u TOM segmentu - ne prosek celog sata snimka,
    vec ono sto se stvarno vidi u ovih ~40 sekundi. To je bitno jer se ljudi
    pomeraju/gestikuliraju, pa prosek celog fajla cesto ne odgovara konkretnom
    isecku. Ako detekcija ne uspe, vraca podrazumevane vrednosti."""
    try:
        cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
        if cascade.empty():
            print("Haar cascade nije ucitan, koristim podrazumevane vrednosti za secenje.")
            return DEFAULT_CROP_LEFT_X, DEFAULT_CROP_RIGHT_X

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("Nisam mogao da otvorim video za detekciju lica, koristim podrazumevane vrednosti.")
            return DEFAULT_CROP_LEFT_X, DEFAULT_CROP_RIGHT_X

        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or int(duration_seconds * fps)

        if frame_w == 0 or frame_h == 0 or total_frames <= 0:
            cap.release()
            print("Neispravni podaci o videu za detekciju lica, koristim podrazumevane vrednosti.")
            return DEFAULT_CROP_LEFT_X, DEFAULT_CROP_RIGHT_X

        range_start = start_seconds if start_seconds is not None else 0.0
        range_end = end_seconds if end_seconds is not None else duration_seconds
        start_frame = max(0, int(range_start * fps))
        end_frame = min(total_frames - 1, int(range_end * fps))
        if end_frame <= start_frame:
            start_frame, end_frame = 0, total_frames - 1

        sample_count = 15
        sample_indices = [
            start_frame + int((end_frame - start_frame) * (i + 1) / (sample_count + 1))
            for i in range(sample_count)
        ]

        half_w = frame_w // 2
        left_centers = []
        right_centers = []

        for idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            left_gray = gray[:, :half_w]
            right_gray = gray[:, half_w:]

            left_faces = cascade.detectMultiScale(left_gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(left_faces) > 0:
                x, y, w, h = max(left_faces, key=lambda r: r[2] * r[3])
                left_centers.append(x + w / 2)

            right_faces = cascade.detectMultiScale(right_gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            if len(right_faces) > 0:
                x, y, w, h = max(right_faces, key=lambda r: r[2] * r[3])
                right_centers.append(half_w + x + w / 2)

        cap.release()

        scale_factor = 1920 / frame_h
        crop_width = 1080

        if left_centers:
            left_centers.sort()
            median_left = left_centers[len(left_centers) // 2]
            left_x = compute_half_crop_offset(median_left, 0, half_w, crop_width, scale_factor)
        else:
            print("Nije pronadjeno lice u levoj polovini ni u jednom frejmu, koristim podrazumevanu vrednost.")
            left_x = DEFAULT_CROP_LEFT_X

        if right_centers:
            right_centers.sort()
            median_right = right_centers[len(right_centers) // 2]
            right_x = compute_half_crop_offset(median_right, half_w, frame_w - half_w, crop_width, scale_factor)
        else:
            print("Nije pronadjeno lice u desnoj polovini ni u jednom frejmu, koristim podrazumevanu vrednost.")
            right_x = DEFAULT_CROP_RIGHT_X

        print(
            f"Detekcija lica gotova: leva pozicija={left_x} ({len(left_centers)}/{sample_count} uspesnih), "
            f"desna pozicija={right_x} ({len(right_centers)}/{sample_count} uspesnih)."
        )
        return left_x, right_x

    except Exception as e:
        print(f"Detekcija lica nije uspela ({e}), koristim podrazumevane vrednosti.")
        return DEFAULT_CROP_LEFT_X, DEFAULT_CROP_RIGHT_X


# ---------- transcription / hook discovery (per file, cached) ----------

def extract_audio(source_path, audio_path, duration_seconds):
    target_bitrate = max(24, min(64, int((23 * 8 * 1024) / duration_seconds)))
    print(f"Izdvajam audio pri {target_bitrate}kbps...")
    cmd = ["ffmpeg", "-y", "-i", source_path, "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Izdvajanje audia nije uspelo.")


def transcribe_audio(audio_path, api_key):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}

    def do_transcribe():
        with open(audio_path, "rb") as f:
            files = {"file": f}
            data = {
                "model": "whisper-1",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            }
            return requests.post(url, headers=headers, files=files, data=data, timeout=600)

    response = retry_request(do_transcribe, "Whisper transkripcija")
    return response.json().get("words", [])


def find_hook_segments(words, api_key, total_duration, n_hooks=HOOKS_PER_FILE):
    lines = [f"[{w['start']:.1f}] {w['word']}" for w in words]
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    prompt = (
        "Ovo je transkript epizode podkasta o licnim finansijama, sa vremenskim oznakama "
        "u sekundama pre svake reci (format [12.3] rec).\n\n"
        f"{transcript_text}\n\n"
        f"Pronadji {n_hooks} RAZLICITIH, NAJSNAZNIJIH, sokantnih ili kontroverznih trenutaka u ovom "
        f"transkriptu, od kojih svaki moze da posluzi kao samostalan kratak klip za drustvene mreze. "
        f"Svaki hook MORA biti stvarno provokativan/iznenadjujuc, ne samo informativan — trazimo reakciju "
        f"'cekaj, sta?!' od gledaoca u prve 3 sekunde. Segmenti ne smeju da se preklapaju.\n\n"
        f"VAZNO - klip mora biti KRATAK I FOKUSIRAN ISKLJUCIVO na tu jednu sok-izjavu: BEZ uvoda, BEZ "
        f"objasnjenja pre nje, BEZ nastavka razgovora posle nje koji vise nije direktno vezan za sok deo. "
        f"Ne biraj ceo tok razgovora - samo taj jedan snazan momenat i njegovu neposrednu okolinu. "
        f"Svaki segment MORA trajati izmedju {MIN_CLIP_SECONDS} i {MAX_CLIP_SECONDS} sekundi (cilj je "
        f"oko 35-40 sekundi), i MORA poceti tacno na pocetku te snazne izjave (ne par recenica ranije). "
        f"Video traje ukupno {total_duration:.0f}s.\n\n"
        "Odgovori ISKLJUCIVO validnim JSON nizom, bez ikakvog dodatnog teksta, u ovom obliku:\n"
        '[{"start": <broj>, "end": <broj>, "reason": "<kratko objasnjenje na srpskom>"}, ...]'
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }

    def do_call():
        return requests.post(url, headers=headers, json=payload, timeout=180)

    response = retry_request(do_call, "Claude hook-detekcija")
    text = response.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    hooks = json.loads(text)
    cleaned = []
    for h in hooks:
        start = max(0.0, float(h["start"]))
        end = min(float(h["end"]), total_duration)
        length = end - start
        if length < MIN_CLIP_SECONDS:
            end = start + MIN_CLIP_SECONDS
        if length > MAX_CLIP_SECONDS:
            end = start + MAX_CLIP_SECONDS
        cleaned.append({"start": start, "end": min(end, total_duration), "reason": h.get("reason", "")})
    print(f"Pronadjeno {len(cleaned)} hook segmenata.")
    return cleaned


def ensure_hooks_for_file(file_info, hooks_cache, openai_key, anthropic_key):
    # Napomena: pozicija secenja lica se NE cuva ovde vise - racuna se posebno za
    # svaki konkretan izabrani isecak (u main()), jer prosek celog fajla ne
    # odgovara uvek konkretnom trenutku koji se koristi za klip.
    file_id = file_info["id"]
    if file_id in hooks_cache and hooks_cache[file_id].get("hooks"):
        return hooks_cache[file_id]

    print(f"Nema keširanih hookova za '{file_info['name']}', pravim transkripciju...")
    tmp_source = f"tmp_{file_id}.mp4"
    tmp_audio = f"tmp_{file_id}.mp3"
    service = get_drive_service()
    download_by_id(service, file_id, tmp_source)
    duration = get_duration_seconds(tmp_source)
    extract_audio(tmp_source, tmp_audio, duration)
    words = transcribe_audio(tmp_audio, openai_key)
    hooks = find_hook_segments(words, anthropic_key, duration)
    os.remove(tmp_source)
    os.remove(tmp_audio)

    hooks_cache[file_id] = {"name": file_info["name"], "duration": duration, "hooks": hooks}
    save_json(HOOKS_CACHE_PATH, hooks_cache)
    return hooks_cache[file_id]


# ---------- segment selection ----------

def pick_next_segment(used_segments, hooks_cache):
    file_ids_sorted = sorted(hooks_cache.keys(), key=lambda fid: len(used_segments.get(fid, [])))
    for fid in file_ids_sorted:
        used = used_segments.get(fid, [])
        used_starts = {round(u[0], 1) for u in used}
        for hook in hooks_cache[fid]["hooks"]:
            if round(hook["start"], 1) not in used_starts:
                return fid, hook
    return None, None


# ---------- captions ----------

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
        for w in words
        if clip_start <= w["start"] < clip_end
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


# ---------- video build (dynamic left/right full-screen crop + watermark + captions) ----------

def build_clip(source_path, watermark_path, captions_path, output_path, start_seconds, length_seconds,
               crop_left_x=DEFAULT_CROP_LEFT_X, crop_right_x=DEFAULT_CROP_RIGHT_X,
               background_audio_path=None, switch_every=3.5):
    filter_parts = [
        "[0:v]scale=3413:1920[scaled];",
        f"[scaled]crop=1080:1920:x='if(lt(mod(t\\,{switch_every*2}),{switch_every}),{crop_left_x},{crop_right_x})':y=0[cropped];",
        "[1:v]scale=220:-1[wm];",
        "[cropped][wm]overlay=W-w-40:H-h-40[pre];",
        f"[pre]ass={captions_path}[outv];",
    ]

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_seconds), "-t", str(length_seconds),
        "-i", source_path,
        "-i", watermark_path,
    ]

    has_bg_audio = bool(background_audio_path) and os.path.exists(background_audio_path)
    if has_bg_audio:
        # -stream_loop -1 pusta pozadinsku muziku u krug ako je kraca od klipa
        cmd += ["-stream_loop", "-1", "-i", background_audio_path]
        filter_parts.append(
            "[0:a]volume=1.0[dlg];"
            f"[2:a]atrim=0:{length_seconds},volume={BACKGROUND_AUDIO_VOLUME}[bg];"
            "[dlg][bg]amix=inputs=2:duration=first:dropout_transition=0[outa];"
        )
        audio_map = ["-map", "[outa]"]
    else:
        audio_map = ["-map", "0:a?"]

    filter_complex = "".join(filter_parts).rstrip(";")

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", *audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    print(f"Pokrecem ffmpeg obradu (secenje x={crop_left_x}/{crop_right_x}, watermark, titlovi"
          f"{', pozadinska muzika' if has_bg_audio else ''})...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFMPEG GRESKA:")
        print(result.stderr[-3000:])
        raise RuntimeError("ffmpeg obrada nije uspela.")
    print("Obrada zavrsena uspesno.")


# ---------- Cloudinary / Instagram ----------

def upload_to_cloudinary(path, cloud_name, upload_preset):
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"

    def do_upload():
        with open(path, "rb") as f:
            files = {"file": f}
            data = {"upload_preset": upload_preset}
            return requests.post(url, files=files, data=data, timeout=300)

    response = retry_request(do_upload, "Cloudinary upload")
    result = response.json()
    return result["secure_url"], result.get("public_id")


def delete_from_cloudinary(public_id, cloud_name, api_key, api_secret):
    if not api_key or not api_secret or not public_id:
        return
    try:
        import hashlib
        timestamp = str(int(time.time()))
        to_sign = f"public_id={public_id}&timestamp={timestamp}{api_secret}"
        signature = hashlib.sha1(to_sign.encode()).hexdigest()
        url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/destroy"
        requests.post(url, data={
            "public_id": public_id, "timestamp": timestamp,
            "api_key": api_key, "signature": signature,
        }, timeout=30)
        print("Cloudinary fajl obrisan (cistoca posle objave).")
    except Exception as e:
        print(f"Nije uspelo brisanje sa Cloudinary-ja (nije kriticno): {e}")


def create_ig_container(ig_user_id, access_token, video_url, caption):
    url = f"https://graph.instagram.com/v23.0/{ig_user_id}/media"
    payload = {"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": access_token}

    def do_create():
        return requests.post(url, data=payload, timeout=60)

    response = retry_request(do_create, "Instagram media container create")
    return response.json()["id"]


def wait_until_ready(creation_id, access_token, max_wait_seconds=600, poll_interval=15):
    url = f"https://graph.instagram.com/v23.0/{creation_id}"
    waited = 0
    while waited < max_wait_seconds:
        response = requests.get(url, params={"fields": "status_code", "access_token": access_token}, timeout=60)
        status = response.json().get("status_code")
        print(f"Status obrade na Instagramu: {status}")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError("Instagram je prijavio gresku pri obradi videa.")
        time.sleep(poll_interval)
        waited += poll_interval
    raise RuntimeError("Instagram obrada videa nije zavrsena u ocekivanom vremenu.")


def publish_container(ig_user_id, access_token, creation_id):
    url = f"https://graph.instagram.com/v23.0/{ig_user_id}/media_publish"
    payload = {"creation_id": creation_id, "access_token": access_token}

    def do_publish():
        return requests.post(url, data=payload, timeout=60)

    response = retry_request(do_publish, "Instagram publish")
    return response.json()["id"]


def main():
    current_hour = datetime.datetime.utcnow().hour
    if current_hour not in ALLOWED_UTC_HOURS:
        print(f"Sat {current_hour} UTC je van dozvoljenog prozora ({min(ALLOWED_UTC_HOURS)}-{max(ALLOWED_UTC_HOURS)}) -> tiho izlazim.")
        return

    daily_counter = load_json(DAILY_COUNTER_PATH, {})
    today = get_today_key()
    if daily_counter.get(today, 0) >= DAILY_TARGET:
        print(f"Dnevni cilj ({DAILY_TARGET}) je vec dostignut za {today} -> tiho izlazim.")
        return

    if not try_claim_lock():
        return

    try:
        service = get_drive_service()
        video_files = list_video_files(service, FOLDER_ID)
        print(f"Pronadjeno {len(video_files)} video fajlova u folderu.")

        download_by_id(service, WATERMARK_FILE_ID, WATERMARK_PATH)

        # Preuzmi sve pozadinske numere (jednom, ostaju keširane za ovaj run),
        # pa nasumicno izaberi jednu za OVAJ konkretan klip - malo raznovrsnosti
        # kroz 10 objava dnevno.
        background_audio_paths = []
        for i, audio_file_id in enumerate(BACKGROUND_AUDIO_FILE_IDS):
            path = f"background_audio_{i}.mp3"
            download_by_id(service, audio_file_id, path)
            background_audio_paths.append(path)
        chosen_background_audio = random.choice(background_audio_paths) if background_audio_paths else None
        if chosen_background_audio:
            print(f"Izabrana pozadinska muzika: {chosen_background_audio}")

        openai_key = os.environ["OPENAI_API_KEY"]
        anthropic_key = os.environ["ANTHROPIC_API_KEY"]

        hooks_cache = load_json(HOOKS_CACHE_PATH, {})
        used_segments = load_json(USED_SEGMENTS_PATH, {})

        for f in video_files:
            ensure_hooks_for_file(f, hooks_cache, openai_key, anthropic_key)

        file_id, hook = pick_next_segment(used_segments, hooks_cache)
        if not hook:
            print("Svi dostupni hookovi iz svih epizoda su vec iskorisceni. Potrebna je nova epizoda u Drive folderu.")
            return

        file_meta = hooks_cache[file_id]
        print(f"Biram segment iz '{file_meta['name']}': {hook['start']:.1f}s -> {hook['end']:.1f}s ({hook['reason']})")

        source_path = f"tmp_{file_id}.mp4"
        download_by_id(service, file_id, source_path)

        transcript_words_path = f"tmp_{file_id}_words.json"
        # re-transcribe only if we don't still have the words cached locally from this run
        audio_path = f"tmp_{file_id}.mp3"
        extract_audio(source_path, audio_path, file_meta["duration"])
        words = transcribe_audio(audio_path, openai_key)

        captions_path = "captions.ass"
        build_captions_file(words, hook["start"], hook["end"], captions_path)

        # Pozicija secenja lica se racuna posebno za OVAJ konkretan isecak (ne
        # prosek celog fajla), jer se ljudi pomeraju/gestikuliraju tokom snimka.
        crop_left_x, crop_right_x = detect_crop_offsets(
            source_path, file_meta["duration"], hook["start"], hook["end"]
        )

        build_clip(
            source_path, WATERMARK_PATH, captions_path, OUTPUT_PATH,
            hook["start"], hook["end"] - hook["start"],
            crop_left_x=crop_left_x,
            crop_right_x=crop_right_x,
            background_audio_path=chosen_background_audio,
        )

        cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
        upload_preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
        video_url, public_id = upload_to_cloudinary(OUTPUT_PATH, cloud_name, upload_preset)

        ig_user_id = os.environ["IG_USER_ID"]
        access_token = os.environ["IG_ACCESS_TOKEN"]

        creation_id = create_ig_container(ig_user_id, access_token, video_url, CAPTION_TEXT)
        wait_until_ready(creation_id, access_token)
        media_id = publish_container(ig_user_id, access_token, creation_id)
        print(f"OBJAVLJENO! Media ID: {media_id}")

        delete_from_cloudinary(
            public_id, cloud_name,
            os.environ.get("CLOUDINARY_API_KEY"), os.environ.get("CLOUDINARY_API_SECRET"),
        )

        used_segments.setdefault(file_id, []).append([hook["start"], hook["end"]])
        daily_counter[today] = daily_counter.get(today, 0) + 1
        save_json(USED_SEGMENTS_PATH, used_segments)
        save_json(DAILY_COUNTER_PATH, daily_counter)
        commit_and_push_state(f"chore: posted clip from {file_meta['name']} ({daily_counter[today]}/{DAILY_TARGET} today)")

        for tmp_file in [source_path, audio_path]:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

    except Exception as e:
        print(f"GRESKA tokom izvrsavanja: {e}")
        raise


if __name__ == "__main__":
    main()
