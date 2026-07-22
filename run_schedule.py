import os
import sys
import json
import time
import random
import signal
import datetime
import subprocess
import requests
import httplib2
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# KRITICNO: kad Python ne radi u terminalu (kao u GitHub Actions), stdout je
# "block-buffered" - sve print() poruke se gomilaju u memoriji i ispisuju tek
# kad se bafer napuni ili proces normalno zavrsi. Ako neko spolja ubije proces
# (SIGTERM/exit 143), taj bafer se NIKAD ne isprazni - sve poruke koje smo
# trebali da vidimo u logu su zauvek izgubljene, i izgleda kao da se kod
# zaglavio odmah na pocetku iako je mozda odmakao mnogo dalje. Ovo je do sada
# skrivalo pravi uzrok svih "run visi, log prazan" slucajeva. Ova linija to
# resava - svaka print() poruka se odmah ispisuje.
sys.stdout.reconfigure(line_buffering=True)

# KRITICNO #2: otkrili smo (posle mnogo pokusaja) da GitHub Actions runner
# povremeno ubija ceo proces spolja sa SIGTERM (exit code 143), bez ikakve
# greske u nasem kodu - resursi (RAM/disk) su potvrdjeno u redu u tim
# trenucima, dakle uzrok je van naseg koda. Problem: kada spolja stigne
# SIGTERM, Python-ov "except Exception" blok u main() NIKAD se ne izvrsi
# (SIGTERM ne postaje Python izuzetak vec odmah prekida proces), pa
# release_lock_after_failure() nikad nije pozvan - zbog toga je katanac
# ostajao "svez" celih 25 minuta posle SVAKOG ovakvog spoljnog ubijanja,
# iako smo mislili da je release-on-failure vec resio to. Ovaj handler to
# ispravlja: hvata SIGTERM/SIGINT eksplicitno i odmah oslobadja katanac
# pre nego sto proces zaista umre.
def _handle_external_shutdown(signum, frame):
    print(f"Primljen signal {signum} (neko/nesto spolja gasi proces) - odmah oslobadjam katanac...")
    try:
        release_lock_after_failure()
    except Exception as e:
        print(f"Nisam uspeo da oslobodim katanac u signal handleru (nije kriticno): {e}")
    sys.exit(143)


signal.signal(signal.SIGTERM, _handle_external_shutdown)
signal.signal(signal.SIGINT, _handle_external_shutdown)

# Podrazumevani timeout-i (u sekundama) za spoljne pozive/procese - bez ovoga,
# mrezni poziv ili ffmpeg koji se "zaglavi" moze da visi zauvek umesto da javi
# gresku (video nam se tacno to desilo - run je "In progress" preko sat vremena).
DRIVE_HTTP_TIMEOUT = 180
GIT_TIMEOUT = 120
FFMPEG_TIMEOUT = 600
FFPROBE_TIMEOUT = 60

FOLDER_ID = "1nrmfGxqCNLH0RdIgzGOV6v_O0aEfcxRb"
WATERMARK_FILE_ID = "1a3FqXNdhtW-QdFq_ww7G_bwdIAUg-7fh"
# Fajlovi koje NIKAD ne treba koristiti (npr. vec sadrze sopstveno secenje/muziku,
# nisu "sirovi" materijal pogodan za nasu automatsku obradu).
EXCLUDED_FILE_IDS = {
    "1aR6bpmKwxWB3HySpcBVZcmeIrKOmrEzs",
    "1OW3tmwxgA1iouG3bVRnmE9soNkwGOprN",  # duplikat drugog vec koriscenog podkasta
}
# Google Drive FILE ID-jevi pozadinske (dramaticne) muzike, bez copyright-a.
# Za svaki objavljeni klip se nasumicno bira JEDNA od ovih numera (radi
# raznovrsnosti). Prazna lista = pozadinska muzika se preskace, nista se ne kvari.
BACKGROUND_AUDIO_FILE_IDS = [
    "1yHsLDQ9yUUe6VtppKUa_MD978Gz7OOHR",
    "1ANHCMAKisUvpxR8KYp0zRnkmKzblj8PN",
]
BACKGROUND_AUDIO_VOLUME = 0.35  # glasnije, dramaticnije - ali i dalje ne prekriva govor
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

STATE_DIR = "state"
USED_SEGMENTS_PATH = f"{STATE_DIR}/used_segments.json"
DAILY_COUNTER_PATH = f"{STATE_DIR}/daily_counter.json"
HOOKS_CACHE_PATH = f"{STATE_DIR}/hooks_cache.json"
LOCK_PATH = f"{STATE_DIR}/lock.txt"

WATERMARK_PATH = "watermark.png"
OUTPUT_PATH = "clip_output.mp4"

MIN_CLIP_SECONDS = 28
MAX_CLIP_SECONDS = 48
MAX_SINGLE_CLIP_SECONDS = 18  # jedna pojedinacna izjava u supercutu ne sme biti duza od ovoga
MIN_CLIP_START_SECONDS = 55  # ne diraj prvih ~55s (vec editovan uvod vlasnika sa muzikom/intro)
HOOKS_PER_FILE = 8
DAILY_TARGET = 10
ALLOWED_UTC_HOURS = set(range(12, 23))  # 12:00 - 22:59 UTC
LOCK_FRESHNESS_MINUTES = 25

DEFAULT_CAPTION_TEXT = (
    "@hgf Real talk you need to hear today. Follow @hgf and listen to the "
    "full episode — link in bio. #HotGirlFinance #MoneyTips #FinanceTok"
)

RETRY_ATTEMPTS = 5
RETRY_DELAYS = [5, 10, 20, 40]


def print_resource_usage(label):
    """Ispisuje trenutno slobodnu RAM i disk memoriju na GitHub Actions
    runneru. Runner proces je nekoliko puta ubijen spolja (exit 143) bas u
    trenutku teskih ffmpeg operacija, bez ikakve greske iz naseg koda - ovo
    ce potvrditi (ili odbaciti) da je uzrok nestanak RAM-a/diska na runneru."""
    try:
        mem = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=10)
        disk = subprocess.run(["df", "-h", "."], capture_output=True, text=True, timeout=10)
        print(f"--- Resursi runnera ({label}) ---")
        print(mem.stdout.strip())
        print(disk.stdout.strip())
        print("--- kraj resursa ---")
    except Exception as e:
        print(f"Nisam uspeo da ocitam resurse runnera (nije kriticno): {e}")


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
    try:
        return subprocess.run(["git"] + cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        print(f"GIT KOMANDA ZAGLAVLJENA (>{GIT_TIMEOUT}s): git {' '.join(cmd)}")
        raise RuntimeError(f"git {' '.join(cmd)} nije zavrsio u {GIT_TIMEOUT}s.") from e


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


def release_lock_after_failure():
    """Kada run legitimno padne sa greskom, oslobodi katanac ODMAH umesto da
    ceka puni LOCK_FRESHNESS_MINUTES prozor - inace svaki neuspeh nepotrebno
    blokira sledeci pokusaj na 25 minuta."""
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
        git_run(["add", LOCK_PATH])
        commit_result = git_run(["commit", "-m", "chore: release lock after failure"])
        if commit_result.returncode != 0 and "nothing to commit" in (commit_result.stdout + commit_result.stderr):
            return
        git_run(["push", "origin", "HEAD"])
        print("Katanac oslobodjen posle greske (sledeci pokusaj ne mora da ceka 25 min).")
    except Exception as e:
        print(f"Nisam uspeo da oslobodim katanac posle greske (nije kriticno, istice sam): {e}")


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
    # httplib2 nema podrazumevani timeout - bez ovoga, jedan zaglavljen mrezni
    # poziv ka Google Drive-u moze da visi zauvek umesto da javi gresku.
    http = httplib2.Http(timeout=DRIVE_HTTP_TIMEOUT)
    authorized_http = AuthorizedHttp(credentials, http=http)
    return build("drive", "v3", http=authorized_http, cache_discovery=False)


def list_video_files(service, folder_id):
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, size, mimeType)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    videos = [
        f for f in files
        if f.get("mimeType", "").startswith("video/") and f["id"] not in EXCLUDED_FILE_IDS
    ]
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
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=FFPROBE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffprobe se zaglavio (>{FFPROBE_TIMEOUT}s) na fajlu {path}.") from e
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


# ---------- transcription / hook discovery (per file, cached) ----------

def extract_audio(source_path, audio_path, duration_seconds):
    target_bitrate = max(24, min(64, int((23 * 8 * 1024) / duration_seconds)))
    print(f"Izdvajam audio pri {target_bitrate}kbps...")
    cmd = ["ffmpeg", "-y", "-i", source_path, "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k", audio_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Izdvajanje audia se zaglavilo (>{FFMPEG_TIMEOUT}s).") from e
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


def snap_time_to_words(target, words, key):
    """Pomera vremensku oznaku na tacnu granicu najblize reci (po Whisper
    timestampu), da se izbegne secenje usred reci na pocetku/kraju isecka."""
    if not words:
        return target
    closest = min(words, key=lambda w: abs(w[key] - target))
    return closest[key]


def find_hook_segments(words, api_key, total_duration, n_hooks=HOOKS_PER_FILE):
    # ne saljemo Claude-u uopste prvih MIN_CLIP_START_SECONDS - to je vec
    # editovan uvod vlasnika (sa muzikom/najavom) i ne sme se dirati/koristiti
    lines = [f"[{w['start']:.1f}] {w['word']}" for w in words if w["start"] >= MIN_CLIP_START_SECONDS]
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    prompt = (
        "Ovo je transkript epizode podkasta o licnim finansijama, sa vremenskim oznakama "
        "u sekundama pre svake reci (format [12.3] rec). Transkript POCINJE tek nakon uvodnog "
        f"dela epizode (prvih {MIN_CLIP_START_SECONDS}s je vec izbaceno) - ne treba dodatno da "
        "brines o tome.\n\n"
        f"{transcript_text}\n\n"
        f"Napravi {n_hooks} RAZLICITIH kratkih 'supercut' klipova za drustvene mreze. Svaki "
        "supercut klip je SASTAVLJEN OD VISE KRATKIH, POJEDINACNIH izjava (recenica ili delova "
        "recenica), spojenih jedna za drugom - NE jedan neprekinuti isecak od 45-75 sekundi bez "
        "secenja. Cilj je maksimalna gustina sadrzaja: SAMO najsokantnije, najbolnije, "
        "najkontroverznije, najemotivnije pojedinacne izjave, bez ikakvog uvoda, objasnjenja ili "
        "'dosadnog' konteksta izmedju njih. Gledalac mora da bude sokiran/zapanjen/emotivno "
        "pogodjen u SVAKOJ pojedinacnoj izjavi, ne samo na pocetku - izbegavaj blage, informativne "
        "ili neutralne recenice.\n\n"
        "PRVA izjava u svakom supercut klipu (hook) mora biti TRENUTNO jasna o cemu se radi, kao "
        "naslov clanka - npr. 'najveca greska koju zene prave sa parama', 'najveca greska u "
        "investiranju koju ljudi prave', ili neka kontroverzna/diskutabilna izjava o odnosima "
        "muskaraca i zena i novca. Gledalac mora u prve 2 sekunde da zna TACNO o cemu je rec i "
        "zasto bi trebalo da gleda dalje.\n\n"
        "Pravila:\n"
        "- Svaka pojedinacna izjava (clip) treba da traje otprilike 4-15 sekundi i MORA poceti "
        "TACNO na pocetku te recenice/izjave (ne usred reci, ne usred nepovezane misli pre nje) i "
        "zavrsiti tacno na kraju te izjave (ne usred sledece recenice).\n"
        "- Kombinuj 3-6 ovakvih pojedinacnih izjava u JEDAN supercut klip.\n"
        "- PONEKAD (ne uvek, samo kad ima smisla) iskoristi PAR pitanje-odgovor: kratko pitanje "
        "voditelja odmah propraceno sokantnim odgovorom sagovornika, kao dve uzastopne izjave u "
        "istom supercutu - to pojacava efekat. Ne mora svaki supercut da ima ovakav par.\n"
        "- Ukupno trajanje svih izjava sabrano u jednom supercut klipu treba da bude izmedju "
        f"{MIN_CLIP_SECONDS} i {MAX_CLIP_SECONDS} sekundi.\n"
        "- Izjave unutar jednog supercut klipa NE moraju biti hronoloski uzastopne u originalnom "
        "snimku - biraj ih sa bilo kog mesta u transkriptu, ali ih poredjaj tako da imaju smisla i "
        "grade utisak/tenziju/payoff kada se puste jedna za drugom bez prekida.\n"
        "- Razliciti supercut klipovi (od ovih {n} koje pravis) ne smeju da koriste iste izjave.\n"
        f"Video traje ukupno {total_duration:.0f}s.\n\n"
        "Za svaki supercut klip napravi i JEDINSTVEN Instagram caption (na engleskom, kratak - "
        "1-2 recenice) koji se konkretno odnosi na TAJ sadrzaj (ne generican tekst koji bi mogao "
        "da stoji ispod bilo kog videa). Caption MORA da sadrzi: pomen '@hgf', poziv da se prati "
        "@hgf i posluša cela epizoda ('link in bio'), i 3-5 relevantnih hashtag-ova (ukljucujuci "
        "#HotGirlFinance). Ne ponavljaj isti caption izmedju razlicitih klipova.\n\n"
        "Odgovori ISKLJUCIVO validnim JSON nizom, bez ikakvog dodatnog teksta, u ovom obliku:\n"
        '[{"clips": [{"start": <broj>, "end": <broj>}, ...], '
        '"reason": "<kratko objasnjenje na srpskom>", '
        '"caption": "<Instagram caption na engleskom>"}, ...]'
    ).replace("{n}", str(n_hooks))

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 3000,
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
            # tvrda zastita (ne samo AI uputstvo) - nikad ne diraj vec editovan
            # uvod vlasnika, bez obzira sta AI vrati
            if start < MIN_CLIP_START_SECONDS:
                continue
            if end - start < 1.0:
                continue
            # ogranici pojedinacnu izjavu na razumnu duzinu - AI ponekad vrati
            # predugacak segment koji nije prava "kratka izjava"
            end = min(end, start + MAX_SINGLE_CLIP_SECONDS)
            # pomeri na tacnu granicu reci + mali sigurnosni razmak da se ne
            # odsece prvi/poslednji glas
            start = max(0.0, snap_time_to_words(start, words, "start") - 0.15)
            end = min(total_duration, snap_time_to_words(end, words, "end") + 0.15)
            if end - start >= 1.0:
                snapped_clips.append([round(start, 2), round(end, 2)])
        if not snapped_clips:
            continue

        # ako je ukupno trajanje predugacko, odseci sa kraja da ne predje razumnu granicu
        hard_ceiling = MAX_CLIP_SECONDS + 15
        total = 0.0
        trimmed = []
        for s, e in snapped_clips:
            length = e - s
            if total + length > hard_ceiling and trimmed:
                break
            trimmed.append([s, e])
            total += length

        caption = h.get("caption", "")
        if not isinstance(caption, str) or "@hgf" not in caption.lower() or len(caption.strip()) < 10:
            # fallback ako AI ne vrati ispravan caption - i dalje moramo imati nesto validno
            caption = DEFAULT_CAPTION_TEXT

        cleaned.append({"clips": trimmed, "reason": h.get("reason", ""), "caption": caption.strip()})

    print(f"Pronadjeno {len(cleaned)} supercut kombinacija (svaka od po nekoliko izjava).")
    return cleaned


def ensure_hooks_for_file(file_info, hooks_cache, openai_key, anthropic_key):
    file_id = file_info["id"]
    cached = hooks_cache.get(file_id)
    cached_hooks = cached.get("hooks") if cached else None
    # stari kes (pre "supercut" formata) nema "clips" kljuc u svakom hooku -
    # takav kes je zastareo i mora se ponovo izracunati, ne moze se ponovo koristiti
    is_current_format = bool(cached_hooks) and all("clips" in h for h in cached_hooks)
    if cached and is_current_format:
        return cached

    print(f"Nema (ili je zastareo) kesiran hook podatak za '{file_info['name']}', pravim transkripciju/analizu...")
    tmp_source = f"tmp_{file_id}.mp4"
    tmp_audio = f"tmp_{file_id}.mp3"
    service = get_drive_service()
    download_by_id(service, file_id, tmp_source)
    duration = get_duration_seconds(tmp_source)
    extract_audio(tmp_source, tmp_audio, duration)
    words = transcribe_audio(tmp_audio, openai_key)
    hooks = find_hook_segments(words, anthropic_key, duration)
    # NAPOMENA: namerno NE brisemo tmp_source ovde (samo audio). Ako se ovaj
    # fajl ispostavi kao onaj koji cemo odmah iskoristiti za supercut, main()
    # ce ga ponovo iskoristiti sa diska umesto da ga preuzima DRUGI PUT -
    # ranije smo isti fajl preuzimali dvaput zaredom (jednom ovde radi
    # transkripcije, pa odmah posle toga opet radi pravljenja klipa), sto je
    # bespotrebno udvostrucavalo vreme/IO svakog run-a.
    os.remove(tmp_audio)

    hooks_cache[file_id] = {"name": file_info["name"], "duration": duration, "hooks": hooks}
    save_json(HOOKS_CACHE_PATH, hooks_cache)
    return hooks_cache[file_id]


# ---------- segment selection ----------

def clip_signature(hook):
    return [round(c[0], 1) for c in hook["clips"]]


def pick_next_segment(used_segments, hooks_cache):
    file_ids_sorted = sorted(hooks_cache.keys(), key=lambda fid: len(used_segments.get(fid, [])))
    for fid in file_ids_sorted:
        if fid in EXCLUDED_FILE_IDS:
            continue
        used_signatures = {tuple(sig) for sig in used_segments.get(fid, [])}
        for hook in hooks_cache[fid]["hooks"]:
            if not hook.get("clips"):
                continue
            if tuple(clip_signature(hook)) not in used_signatures:
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


# ---------- video build ----------

def build_supercut(source_path, clips, output_path):
    """Iseca vise kratkih pojedinacnih izjava iz izvornog snimka i spaja ih u
    JEDAN neprekinuti video (supercut) - jos bez uokvirivanja/vodenog zig/titlova,
    to se radi u sledecem koraku."""
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
        "ffmpeg", "-y",
        "-i", source_path,
        "-filter_complex", filter_complex,
        "-map", "[vcat]", "-map", "[acat]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    print(f"Spajam {n} kratkih izjava u jedan supercut...")
    print_resource_usage("pre spajanja supercuta")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Spajanje izjava se zaglavilo (>{FFMPEG_TIMEOUT}s).") from e
    if result.returncode != 0:
        print("FFMPEG GRESKA (spajanje izjava):")
        print(result.stderr[-3000:])
        raise RuntimeError("Spajanje izjava nije uspelo.")
    print("Izjave uspesno spojene.")


def finalize_clip(input_path, watermark_path, captions_path, output_path, background_audio_path=None):
    """Uzima vec spojeni supercut i: (1) uokviruje ga u 9:16 tako da se CEO
    16:9 kadar vidi (oba lica uvek potpuno vidljiva), sa blurovanom uvecanom
    pozadinom umesto secenja, (2) dodaje watermark, (3) narice titlove,
    (4) po zelji umeksa pozadinsku muziku."""
    filter_parts = [
        "[0:v]split=2[bg][fg];",
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bgblur];",
        "[fg]scale=1080:-2:force_original_aspect_ratio=decrease[fgscaled];",
        "[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2[framed];",
        "[1:v]scale=220:-1[wm];",
        "[framed][wm]overlay=W-w-40:H-h-40[pre];",
        f"[pre]ass={captions_path}[outv];",
    ]

    cmd = ["ffmpeg", "-y", "-i", input_path, "-i", watermark_path]

    has_bg_audio = bool(background_audio_path) and os.path.exists(background_audio_path)
    if has_bg_audio:
        # -stream_loop -1 pusta pozadinsku muziku u krug ako je kraca od klipa;
        # amix sa duration=first automatski skrati na duzinu govora.
        cmd += ["-stream_loop", "-1", "-i", background_audio_path]
        filter_parts.append(
            "[0:a]volume=1.0[dlg];"
            f"[2:a]volume={BACKGROUND_AUDIO_VOLUME}[bgaud];"
            "[dlg][bgaud]amix=inputs=2:duration=first:dropout_transition=0[outa];"
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
    print(f"Zavrsna obrada (16:9 sa blur pozadinom, watermark, titlovi"
          f"{', pozadinska muzika' if has_bg_audio else ''})...")
    print_resource_usage("pre zavrsne obrade")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Zavrsna obrada se zaglavila (>{FFMPEG_TIMEOUT}s).") from e
    if result.returncode != 0:
        print("FFMPEG GRESKA:")
        print(result.stderr[-3000:])
        raise RuntimeError("Zavrsna obrada nije uspela.")
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

        # KLJUCNA PROMENA (ovo je bio pravi uzrok "run traje jako dugo, katanac
        # ostaje svez, niko ne objavi nista"): ranije smo racunali hookove za
        # SVE fajlove u folderu pre nego sto bismo uopste probali da objavimo
        # bilo sta. Za svaki fajl bez keširanih hookova to znaci: preuzimanje
        # celog videa + Whisper transkripcija CELE epizode + Claude analiza -
        # to lako potraje vise minuta PO FAJLU. Ako folder ima vise epizoda bez
        # keša (npr. posle reseta hooks_cache.json), ceo run bi trajao i preko
        # sat vremena pre nego sto bi uopste stigao do objavljivanja, drzeci
        # katanac "svez" sve to vreme dok drugi pokusaji korektno cekaju.
        #
        # Sada radimo fajl po fajl i cim nadjemo fajl koji ima slobodan
        # (neiskoriscen) segment, ODMAH prekidamo petlju i idemo na
        # objavljivanje. Preostali fajlovi ce dobiti hookove u nekom od
        # sledecih pokretanja (kes se cuva posle SVAKOG fajla, tako da se
        # napredak nikad ne gubi).
        file_id, hook = None, None
        for f in video_files:
            if f["id"] in EXCLUDED_FILE_IDS:
                continue
            ensure_hooks_for_file(f, hooks_cache, openai_key, anthropic_key)
            save_json(HOOKS_CACHE_PATH, hooks_cache)

            candidate_id, candidate_hook = pick_next_segment(used_segments, hooks_cache)
            if candidate_hook:
                file_id, hook = candidate_id, candidate_hook
                break

        commit_and_push_state("chore: update hooks cache")

        if not hook:
            print("Svi dostupni hookovi iz svih epizoda su vec iskorisceni. Potrebna je nova epizoda u Drive folderu.")
            return

        file_meta = hooks_cache[file_id]
        clips = hook["clips"]
        total_len = sum(e - s for s, e in clips)
        print(
            f"Biram supercut iz '{file_meta['name']}': {len(clips)} izjava, "
            f"ukupno ~{total_len:.1f}s ({hook['reason']})"
        )

        source_path = f"tmp_{file_id}.mp4"
        if os.path.exists(source_path):
            # vec preuzet malopre unutar ensure_hooks_for_file() za ovaj isti
            # fajl - ne preuzimaj ga opet, to samo udvostrucuje vreme/IO
            print(f"'{file_meta['name']}' je vec preuzet malopre, ne preuzimam ponovo.")
        else:
            download_by_id(service, file_id, source_path)
        print_resource_usage("posle preuzimanja izvornog videa")

        supercut_path = f"tmp_{file_id}_supercut.mp4"
        build_supercut(source_path, clips, supercut_path)

        # Transkribuj CIST spojeni supercut (pre mesanja sa pozadinskom muzikom,
        # radi tacnijih titlova) - njegov sopstveni 0-bazirani vremenski tok.
        supercut_audio_path = f"tmp_{file_id}_supercut.mp3"
        supercut_duration = get_duration_seconds(supercut_path)
        extract_audio(supercut_path, supercut_audio_path, supercut_duration)
        words = transcribe_audio(supercut_audio_path, openai_key)

        captions_path = "captions.ass"
        build_captions_file(words, 0.0, supercut_duration + 1.0, captions_path)

        finalize_clip(
            supercut_path, WATERMARK_PATH, captions_path, OUTPUT_PATH,
            background_audio_path=chosen_background_audio,
        )

        cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
        upload_preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
        video_url, public_id = upload_to_cloudinary(OUTPUT_PATH, cloud_name, upload_preset)

        ig_user_id = os.environ["IG_USER_ID"]
        access_token = os.environ["IG_ACCESS_TOKEN"]

        post_caption = hook.get("caption") or DEFAULT_CAPTION_TEXT
        print(f"Caption za ovu objavu: {post_caption}")
        creation_id = create_ig_container(ig_user_id, access_token, video_url, post_caption)
        wait_until_ready(creation_id, access_token)
        media_id = publish_container(ig_user_id, access_token, creation_id)
        print(f"OBJAVLJENO! Media ID: {media_id}")

        delete_from_cloudinary(
            public_id, cloud_name,
            os.environ.get("CLOUDINARY_API_KEY"), os.environ.get("CLOUDINARY_API_SECRET"),
        )

        used_segments.setdefault(file_id, []).append(clip_signature(hook))
        daily_counter[today] = daily_counter.get(today, 0) + 1
        save_json(USED_SEGMENTS_PATH, used_segments)
        save_json(DAILY_COUNTER_PATH, daily_counter)
        commit_and_push_state(f"chore: posted clip from {file_meta['name']} ({daily_counter[today]}/{DAILY_TARGET} today)")

        for tmp_file in [source_path, supercut_path, supercut_audio_path]:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

    except Exception as e:
        print(f"GRESKA tokom izvrsavanja: {e}")
        release_lock_after_failure()
        raise


if __name__ == "__main__":
    main()
    fix lock release
