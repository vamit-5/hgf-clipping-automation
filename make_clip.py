import os
import json
import subprocess
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


def main():
    service = get_drive_service()

    print(f"Trazim izvorni fajl koji sadrzi: '{SOURCE_NAME_CONTAINS}'")
    source_info = find_file(service, FOLDER_ID, SOURCE_NAME_CONTAINS)
    print(f"Pronadjen: '{source_info['name']}' -> preuzimam (ili koristim kes)...")
    download_by_id(service, source_info["id"], SOURCE_PATH)

    print("Preuzimam watermark...")
    download_by_id(service, WATERMARK_FILE_ID, WATERMARK_PATH)

    build_clip(SOURCE_PATH, WATERMARK_PATH, OUTPUT_PATH, CLIP_START_SECONDS, CLIP_LENGTH_SECONDS)

    size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)
    print(f"Gotov klip: {OUTPUT_PATH} (~{size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
