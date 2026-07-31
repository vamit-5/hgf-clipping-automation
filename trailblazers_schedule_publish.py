import os
import sys
import json
import time
import subprocess
import datetime
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ---------------------------------------------------------------------------
# Trailblazers x Ann Miura-Ko - DEO 2 od 2: OBJAVLJUJE na Instagram.
# Ovaj skript se NAMERNO izvrsava na GitHub-ovom cloud serveru
# (ubuntu-latest), NE na self-hosted runneru (kucni racunar) - jer je
# identican, potpuno ispravan Instagram token USPESNO radio kad je pozvan
# sa GitHub cloud IP adrese, ali je DOSLEDNO padao sa "session invalidated"
# kad je pozvan sa kucne mreze. Video i podaci za objavu (final_reel.mp4,
# publish_data.json) stizu ovde kao GitHub Actions "artifact" iz prvog
# posla (trailblazers_schedule.py, koji se i dalje izvrsava na self-hosted
# runneru jer treba OpenCV/Remotion/ffmpeg).
# Nakon uspesne objave, ovaj skript SAM upisuje iskorisceni segment i
# povecava dnevni brojac (git commit + push) - to se namerno NE radi u
# prvom poslu, da se tema ne oznaci kao "iskoriscena" ako objava nikad
# nije uspela.
# ---------------------------------------------------------------------------

OUTPUT_DIR = "output_clips_schedule"
FINAL_VIDEO_PATH = os.path.join(OUTPUT_DIR, "final_reel.mp4")
PUBLISH_DATA_PATH = os.path.join(OUTPUT_DIR, "publish_data.json")

STATE_DIR = "state"
USED_SEGMENTS_PATH = os.path.join(STATE_DIR, "trailblazers_used_segments.json")
DAILY_COUNTER_PATH = os.path.join(STATE_DIR, "trailblazers_daily_counter.json")

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


def run_git(args, check=True):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=check,
    )


def record_success(clips, hook_label):
    """Upisuje iskorisceni segment i povecava dnevni brojac, pa komituje i
    push-uje te izmene nazad u repo (retry sa fetch+rebase, isti obrazac
    kao git-baziran katanac u prvom poslu)."""
    run_git(["fetch", "origin"], check=False)
    run_git(["reset", "--hard", "origin/main"], check=False)

    used = load_json(USED_SEGMENTS_PATH, [])
    used.append({"clips": clips, "hook": hook_label, "at": datetime.datetime.utcnow().isoformat()})
    save_json(USED_SEGMENTS_PATH, used)

    counter = load_json(DAILY_COUNTER_PATH, {"date": today_str(), "count": 0})
    if counter.get("date") != today_str():
        counter = {"date": today_str(), "count": 0}
    counter["count"] = int(counter.get("count", 0)) + 1
    save_json(DAILY_COUNTER_PATH, counter)

    run_git(["add", USED_SEGMENTS_PATH, DAILY_COUNTER_PATH], check=False)
    commit = run_git(["commit", "-m", "Zabelezi uspesnu Trailblazers objavu"], check=False)
    if commit.returncode != 0:
        print("[stanje] Nista za komit (neobicno, ali nije fatalno).")
        return

    for attempt in range(5):
        run_git(["fetch", "origin"], check=False)
        rebase = run_git(["rebase", "origin/main"], check=False)
        if rebase.returncode != 0:
            run_git(["rebase", "--abort"], check=False)
            time.sleep(3)
            continue
        push = run_git(["push", "origin", "main"], check=False)
        if push.returncode == 0:
            print(f"[cilj] Objavljeno {counter['count']} danas - stanje sacuvano.")
            return
        time.sleep(3)
    print("[stanje] UPOZORENJE: nisam uspeo da sacuvam dnevni brojac/iskorisceni segment posle 5 pokusaja.")


# --------------------------- Instagram objavljivanje -------------------------

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

    response = retry_request(do_create, "IG container kreiranje")
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
    if not os.path.exists(FINAL_VIDEO_PATH) or not os.path.exists(PUBLISH_DATA_PATH):
        raise RuntimeError(
            f"Nedostaju fajlovi iz prvog posla ({FINAL_VIDEO_PATH} / {PUBLISH_DATA_PATH}). "
            "Proveri da li je artifact ispravno preuzet."
        )

    publish_data = load_json(PUBLISH_DATA_PATH, None)
    if not publish_data:
        raise RuntimeError(f"{PUBLISH_DATA_PATH} je prazan ili nevalidan.")

    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    upload_preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
    ig_user_id = os.environ["IG_USER_ID"]
    access_token = os.environ["IG_ACCESS_TOKEN"]

    video_url = upload_to_cloudinary(FINAL_VIDEO_PATH, cloud_name, upload_preset)
    creation_id = create_ig_container(ig_user_id, access_token, video_url, publish_data["caption"])
    wait_until_ready(creation_id, access_token)
    publish_container(ig_user_id, access_token, creation_id)

    record_success(publish_data["clips"], publish_data["hook_label"])


if __name__ == "__main__":
    main()
