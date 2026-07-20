import os
import json
import subprocess
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
NAME_CONTAINS = "Maria Vardag"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
LOCAL_PATH = "source_test.mp4"


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
    print(f"Ukupno stavki u folderu: {len(files)}")
    matches = [f for f in files if name_contains.lower() in f["name"].lower()]
    if not matches:
        print("Sva imena fajlova koja postoje u folderu:")
        for f in files:
            print(f"   -> '{f['name']}'")
        raise RuntimeError(f"Nijedan fajl ne sadrzi '{name_contains}' u imenu.")
    return matches[0]


def download_file(service, file_id, destination):
    request = service.files().get_media(fileId=file_id)
    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Preuzeto: {int(status.progress() * 100)}%")


def probe_video(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,r_frame_rate:stream_tags=rotate:side_data=rotation",
        "-of", "json",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    print("ffprobe stdout:")
    print(result.stdout)
    if result.stderr:
        print("ffprobe stderr:")
        print(result.stderr)


def main():
    service = get_drive_service()
    print(f"Trazim fajl koji sadrzi: '{NAME_CONTAINS}'")
    file_info = find_file(service, FOLDER_ID, NAME_CONTAINS)
    size_gb = int(file_info.get("size", 0)) / (1024**3)
    print(f"Pronadjen: '{file_info['name']}' (~{size_gb:.2f} GB) -> preuzimam...")
    download_file(service, file_info["id"], LOCAL_PATH)
    print("Preuzimanje zavrseno. Pokrecem ffprobe...")
    probe_video(LOCAL_PATH)
    actual_size = os.path.getsize(LOCAL_PATH) / (1024**3)
    print(f"Velicina preuzetog fajla na disku: {actual_size:.2f} GB")


if __name__ == "__main__":
    main()
