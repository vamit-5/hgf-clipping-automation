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
OUTPUT_PATH = "clip_output.mp4"

CLIP_START_SECONDS = 120
CLIP_LENGTH_SECONDS = 60

CAPTION = (
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
                print(f"[{description}] TRAJNA GRESKA {response.status_code}: {response.text}")
                raise RuntimeError(f"{description} nije uspeo (trajna greska {response.status_code}).")
            print(f"[{description}] Privremena greska {response.status_code}, pokusaj {attempt}/{RETRY_ATTEMPTS}")
            last_error = RuntimeError(f"{description}: {response.status_code} {response.text}")
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


def build_clip(source_path, watermark_path, output_path, start_seconds, length_seconds):
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[merged];"
        "[1:v]scale=220:-1[wm];"
        "[merged][wm]overlay=W-w-40:H-h-40[outv]"
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
    print("Pokrecem ffmpeg obradu...")
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
    result = response.json()
    secure_url = result["secure_url"]
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
        response = requests.get(
            url, params={"fields": "status_code", "access_token": access_token}, timeout=60
        )
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

    build_clip(SOURCE_PATH, WATERMARK_PATH, OUTPUT_PATH, CLIP_START_SECONDS, CLIP_LENGTH_SECONDS)

    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    upload_preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
    video_url = upload_to_cloudinary(OUTPUT_PATH, cloud_name, upload_preset)

    ig_user_id = os.environ["IG_USER_ID"]
    access_token = os.environ["IG_ACCESS_TOKEN"]

    creation_id = create_ig_container(ig_user_id, access_token, video_url, CAPTION)
    wait_until_ready(creation_id, access_token)
    publish_container(ig_user_id, access_token, creation_id)

    print("GOTOVO.")


if __name__ == "__main__":
    main()
