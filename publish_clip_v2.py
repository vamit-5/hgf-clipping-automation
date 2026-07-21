import os
import json
import time
import subprocess
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
SOURCE_NAME_CONTAINS = "Maria Vardag"
WATERMARK_FILE_ID = "1a3FqXNdhtW-QdFq_ww7G_bwdIAUg-7fh"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

SOURCE_PATH = "source.mp4"
WATERMARK_PATH = "watermark.png"
AUDIO_PATH = "audio.mp3"
TRANSCRIPT_PATH = "transcript.json"
CAPTIONS_PATH = "captions.ass"
OUTPUT_PATH = "clip_output.mp4"

MIN_CLIP_SECONDS = 45
MAX_CLIP_SECONDS = 75

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


def get_drive_service():
    creds_info = json.loads(os.environ["GDRIVE_CREDENTIALS_JSON"])
    credentials = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=credentials)


def find_file(service, folder_id, name_contains):
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, size, mimeType)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    matches = [f for f in files if name_contains.lower() in f["name"].lower()]
    if not matches:
        raise RuntimeError(f"Nijedan fajl ne sadrzi '{name_contains}' u imenu.")
    return matches[0]


def download_by_id(service, file_id, destination):
    if os.path.exists(destination):
        print(f"{destination} vec postoji (iz kesa), preskacem preuzimanje.")
        return
    request = service.files().get_media(fileId=file_id)
    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Preuzeto: {int(status.progress() * 100)}%")


def get_duration_seconds(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def extract_audio(source_path, audio_path, duration_seconds):
    if os.path.exists(audio_path):
        print(f"{audio_path} vec postoji (iz kesa), preskacem izdvajanje audia.")
        return
    target_bitrate = int((23 * 8 * 1024) / duration_seconds)
    target_bitrate = max(24, min(64, target_bitrate))
    print(f"Izdvajam audio pri {target_bitrate}kbps (trajanje {duration_seconds:.0f}s)...")
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise RuntimeError("Izdvajanje audia nije uspelo.")
    size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    print(f"Audio fajl: ~{size_mb:.1f} MB")
    if size_mb > 24.5:
        raise RuntimeError(f"Audio fajl je i dalje prevelik ({size_mb:.1f} MB) za Whisper API limit od 25MB.")


def transcribe_audio(audio_path, api_key):
    if os.path.exists(TRANSCRIPT_PATH):
        print(f"{TRANSCRIPT_PATH} vec postoji (iz kesa), preskacem transkripciju.")
        with open(TRANSCRIPT_PATH) as f:
            return json.load(f)

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
    result = response.json()
    words = result.get("words", [])
    print(f"Transkripcija zavrsena: {len(words)} reci.")
    with open(TRANSCRIPT_PATH, "w") as f:
        json.dump(words, f)
    return words


def find_hook_segment(words, api_key, total_duration):
    lines = []
    for w in words:
        lines.append(f"[{w['start']:.1f}] {w['word']}")
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    prompt = (
        "Ovo je transkript epizode podkasta o licnim finansijama, sa vremenskim oznakama "
        "u sekundama pre svake reci (format [12.3] rec).\n\n"
        f"{transcript_text}\n\n"
        f"Pronadji NAJSNAZNIJI, najsokantniji ili najprovokativniji trenutak u ovom transkriptu koji "
        f"moze da posluzi kao 'hook' (kuka za paznju) na pocetku kratkog klipa za drustvene mreze. "
        f"Klip mora trajati izmedju {MIN_CLIP_SECONDS} i {MAX_CLIP_SECONDS} sekundi, i MORA poceti "
        f"TACNO na pocetku te snazne izjave (ne par sekundi pre ili posle). Video traje ukupno "
        f"{total_duration:.0f} sekundi, pa 'end' ne sme preci tu vrednost.\n\n"
        "Odgovori ISKLJUCIVO validnim JSON objektom, bez ikakvog dodatnog teksta, u ovom obliku:\n"
        '{"start": <broj_sekundi>, "end": <broj_sekundi>, "reason": "<kratko objasnjenje na srpskom>"}'
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }

    def do_call():
        return requests.post(url, headers=headers, json=payload, timeout=120)

    response = retry_request(do_call, "Claude hook-detekcija")
    result = response.json()
    text = result["content"][0]["text"].strip()
    print(f"Claude odgovor: {text}")

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    start = max(0.0, float(parsed["start"]))
    end = float(parsed["end"])
    length = end - start
    if length < MIN_CLIP_SECONDS:
        end = start + MIN_CLIP_SECONDS
    if length > MAX_CLIP_SECONDS:
        end = start + MAX_CLIP_SECONDS
    end = min(end, total_duration)
    print(f"Izabran hook segment: {start:.1f}s -> {end:.1f}s ({parsed.get('reason', '')})")
    return start, end


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
Style: Caption,Liberation Sans,74,&H00FFFFFF,&H00000000,&H00000000,1,0,1,6,0,2,60,60,260

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
    print(f"Napravljeno {len(groups)} caption kartica.")


def build_clip(source_path, watermark_path, captions_path, output_path, start_seconds, length_seconds):
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[merged];"
        "[1:v]scale=220:-1[wm];"
        "[merged][wm]overlay=W-w-40:H-h-40[pre];"
        f"[pre]ass={captions_path}[outv]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_seconds), "-t", str(length_seconds),
        "-i", source_path,
        "-i", watermark_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    print("Pokrecem ffmpeg obradu (secenje + watermark + vertikalni format + titlovi)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFMPEG GRESKA:")
        print(result.stderr[-3000:])
        raise RuntimeError("ffmpeg obrada nije uspela.")
    print("Obrada zavrsena uspesno.")


def upload_to_cloudinary(path, cloud_name, upload_preset):
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"

    def do_upload():
        with open(path, "rb") as f:
            files = {"file": f}
            data = {"upload_preset": upload_preset}
            return requests.post(url, files=files, data=data, timeout=300)

    response = retry_request(do_upload, "Cloudinary upload")
    secure_url = response.json()["secure_url"]
    print(f"Cloudinary URL: {secure_url}")
    return secure_url


def create_ig_container(ig_user_id, access_token, video_url, caption):
    url = f"https://graph.instagram.com/v23.0/{ig_user_id}/media"
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": access_token,
    }

    def do_create():
        return requests.post(url, data=payload, timeout=60)

    response = retry_request(do_create, "Instagram media container create")
    creation_id = response.json()["id"]
    print(f"Instagram creation_id: {creation_id}")
    return creation_id


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
    media_id = response.json()["id"]
    print(f"OBJAVLJENO! Media ID: {media_id}")
    return media_id


def main():
    service = get_drive_service()

    print(f"Trazim izvorni fajl koji sadrzi: '{SOURCE_NAME_CONTAINS}'")
    source_info = find_file(service, FOLDER_ID, SOURCE_NAME_CONTAINS)
    print(f"Pronadjen: '{source_info['name']}' -> preuzimam (ili koristim kes)...")
    download_by_id(service, source_info["id"], SOURCE_PATH)

    print("Preuzimam watermark...")
    download_by_id(service, WATERMARK_FILE_ID, WATERMARK_PATH)

    total_duration = get_duration_seconds(SOURCE_PATH)
    print(f"Ukupno trajanje izvora: {total_duration:.0f}s")

    extract_audio(SOURCE_PATH, AUDIO_PATH, total_duration)

    openai_key = os.environ["OPENAI_API_KEY"]
    words = transcribe_audio(AUDIO_PATH, openai_key)

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    start, end = find_hook_segment(words, anthropic_key, total_duration)

    build_captions_file(words, start, end, CAPTIONS_PATH)

    build_clip(SOURCE_PATH, WATERMARK_PATH, CAPTIONS_PATH, OUTPUT_PATH, start, end - start)

    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    upload_preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
    video_url = upload_to_cloudinary(OUTPUT_PATH, cloud_name, upload_preset)

    ig_user_id = os.environ["IG_USER_ID"]
    access_token = os.environ["IG_ACCESS_TOKEN"]

    creation_id = create_ig_container(ig_user_id, access_token, video_url, CAPTION_TEXT)
    wait_until_ready(creation_id, access_token)
    publish_container(ig_user_id, access_token, creation_id)

    print("GOTOVO.")


if __name__ == "__main__":
    main()
