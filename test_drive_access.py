import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

DRIVE_FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    creds_json = os.environ["GDRIVE_CREDENTIALS_JSON"]
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials)


def list_video_files(service, folder_id):
    query = f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false"
    results = service.files().list(
        q=query,
        fields="files(id, name, size, mimeType, modifiedTime)",
        pageSize=100,
    ).execute()
    return results.get("files", [])


def main():
    print("Povezujem se na Google Drive...")
    service = get_drive_service()
    print("Trazim video fajlove u HGF folderu...")
    files = list_video_files(service, DRIVE_FOLDER_ID)
    if not files:
        print("NIJE PRONADJEN NIJEDAN VIDEO FAJL. Proveri da li je folder javno dostupan ili da li servisni nalog ima pristup.")
        return
    print(f"Pronadjeno {len(files)} video fajl(ova):")
    for f in files:
        size_bytes = int(f.get("size", 0)) if f.get("size") else 0
        size_mb = size_bytes / (1024 * 1024)
        print(f" - {f['name']} (ID: {f['id']}, ~{size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
