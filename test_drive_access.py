import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DRIVE_FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    creds_json = os.environ["GDRIVE_CREDENTIALS_JSON"]
    creds_info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    print(f"Servisni nalog koji koristimo: {creds_info.get('client_email')}")
    return build("drive", "v3", credentials=credentials)


def main():
    service = get_drive_service()

    print("\n--- Test 1: Da li mozemo da vidimo SAM folder? ---")
    try:
        folder = service.files().get(
            fileId=DRIVE_FOLDER_ID,
            fields="id, name, mimeType",
            supportsAllDrives=True,
        ).execute()
        print(f"USPEH: Folder pronadjen -> ime: '{folder.get('name')}', tip: {folder.get('mimeType')}")
    except HttpError as e:
        print(f"GRESKA pri pristupu folderu: {e}")
        print("Ovo najverovatnije znaci da servisni nalog NEMA dozvolu da vidi ovaj folder.")
        return

    print("\n--- Test 2: Sve stavke unutar foldera (bez filtera po tipu) ---")
    try:
        results = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
            fields="files(id, name, mimeType, size)",
            pageSize=200,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items = results.get("files", [])
        if not items:
            print("Folder je vidljiv, ali API ne vraca NIJEDNU stavku unutra (moguce da su dozvole ogranicene na sam folder bez sadrzaja, ili je folder prazan za ovaj nalog).")
        else:
            print(f"Pronadjeno {len(items)} stavki:")
            for it in items:
                size = it.get("size")
                size_txt = f", ~{int(size) / (1024*1024):.1f} MB" if size else ""
                print(f" - {it['name']}  [{it['mimeType']}]{size_txt}")
    except HttpError as e:
        print(f"GRESKA pri listanju sadrzaja: {e}")


if __name__ == "__main__":
    main()
