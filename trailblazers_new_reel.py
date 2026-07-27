import os
import sys
import json
import time
import random
import subprocess
import shutil
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ---------------------------------------------------------------------------
# Trailblazers x Ann Miura-Ko - JEDAN NOVI reel, po zvanicnom brief-u.
# - PROMENA (run #11): umesto da skripta sama skida video preko WeTransfer-a
# (nepouzdano, linkovi isticu/menjaju format), korisnik rucno stavlja
# preuzeti video fajl kao "annmiura_source.mp4" u working-directory
# runnera PRE pokretanja - skripta to prepoznaje i preskace preuzimanje.
# WeTransfer kod je ostavljen kao fallback ako fajl slucajno ne postoji.
# - PROMENA (run #12): AKO Claude ne pronadje temu u transkriptu, skripta
# VISE NE PUCA sa RuntimeError (sto je gasilo ceo GitHub Actions job kao
# FAILED). Skripta sada to jasno ispise i izadje NORMALNO (exit code 0),
# bez rusenja builda.
# - PROMENA (run #13): CONCEPT_DESCRIPTION sa jednom uskom, hardkodovanom
# temom (vezanom za naslov TACNO JEDNE epizode) zamenjena je opstim,
# trajnim kriterijumima sadrzaja (CONTENT_CRITERIA) koji vaze za SVAKU
# epizodu ovog podkasta - po zvanicnom brief-u odobrene su TRI vrste
# klipova: standalone advice (prioritet), hot-take/kontrarian, i story
# clip. Ovo drasticno povecava sanse da se u SVAKOJ epizodi pronadje
# validan klip, umesto da zavisi od toga da li se pominje bas jedna
# konkretna, unapred zadata tema.
# - ostar, pun 9:16 kadar, cist rez bez "mrtvog vazduha"
# - dramaticna pozadinska muzika (Google Drive fajlovi kao HGF pipeline)
# - mali xfade/acrossfade tranzicioni efekti izmedju spojenih izjava
# - FINALNI RENDER radi REMOTION (animirani brending, titlovi, muzika) -
# eventualni dodatni efekti (zoom in/out, zvucni efekti) zive u Remotion
# komponenti (remotion/trailblazers/src/...), NE u ovom fajlu.
# - NAMERNO NE OBJAVLJUJE automatski na Instagram - samo pravi klip za
# pregled.
# ---------------------------------------------------------------------------

WETRANSFER_SHORT_URL = "https://we.tl/t-CNSBnb2WgM3qgNGe"
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
BACKGROUND_AUDIO_VOLUME = 0.45
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

REMOTION_ENTRY = "src/index.ts"  # relativno na REMOTION_WORKDIR (cwd), ne od korena repoa
REMOTION_COMPOSITION_ID = "TrailblazersReel"
REMOTION_WORKDIR = "remotion/trailblazers"

# Ovi kriterijumi vaze za SVAKU epizodu (nisu vezani za jednu konkretnu temu).
# Bazirano na zvanicnom brief-u: sta je odobreno za Trailblazers reel-ove.
CONTENT_CRITERIA = (
    "Trazis TACNO JEDAN 'supercut' klip (sastavljen od 3-5 kratkih pojedinacnih izjava "
    "spojenih jedna za drugom) koji spada u JEDNU od sledece tri odobrene kategorije. "
    "Prva kategorija je prioritetna/primarni izbor - ako postoji dobar kandidat za nju, "
    "uzmi nju. Ako ne postoji, probaj drugu ili trecu kategoriju, tim redosledom.\n\n"
    "1) STANDALONE ADVICE (PRIORITET) - Ann objasnjava JEDNU jasnu ideju ili lekciju "
    "od pocetka do kraja, potpuno razumljivu samu za sebe, bez potrebe za dodatnim "
    "kontekstom iz ostatka epizode. Primer: sta je 'thunder lizard' osnivac, kako "
    "prepoznati product-market fit.\n\n"
    "2) HOT-TAKE / KONTRARIAN STAV - Ann se jasno suprotstavlja opste prihvacenom "
    "misljenju, iznosi stav koji lomi ocekivanja (npr. 'najbolje ideje na prvi pogled "
    "deluju pogresno'). Mora biti prepoznatljiv, jasan 'pattern-breaking' momenat.\n\n"
    "3) STORY CLIP - licni, ljudski momenat koji dokazuje neku lekciju ili je sam po "
    "sebi upecatljiva, autenticna prica (npr. zavrsila je doktorat nedelju dana posle "
    "porodjaja dok je pokretala fond). Mora delovati relatable i stvarno, ne kao suva "
    "cinjenica.\n\n"
    "NIJE prihvatljivo (nemoj birati ovakve segmente):\n"
    "- Nasumican ili nepovezan segment bez jasnog, prepoznatljivog momenta iz intervjua.\n"
    "- Segment koji se ne moze razumeti kao celina bez siroko konteksta iz ostatka "
    "epizode (gledalac mora odmah da 'ukapira' o cemu je rec).\n"
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


def download_from_wetransfer(short_url, destination):
    print(f"Pratim WeTransfer redirekt sa {short_url}...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})

    resp = session.get(short_url, allow_redirects=True, timeout=60)
    final_url = resp.url
    print(f"Zavrsni URL: {final_url}")

    parts = final_url.split("wetransfer.com/downloads/")[-1].split("?")[0].strip("/").split("/")
    if len(parts) < 2:
        raise RuntimeError(f"Nisam uspeo da izvucem transfer_id/security_hash iz URL-a: {final_url}")
    transfer_id, security_hash = parts[0], parts[1]
    print(f"transfer_id={transfer_id} security_hash={security_hash}")

    api_url = f"https://wetransfer.com/api/v4/transfers/{transfer_id}/download"
    payload = {"security_hash": security_hash, "intent": "entire_transfer"}

    def do_call():
        return session.post(api_url, json=payload, timeout=60)

    api_resp = retry_request(do_call, "WeTransfer API poziv")
    direct_link = api_resp.json().get("direct_link")
    if not direct_link:
        raise RuntimeError(f"WeTransfer API nije vratio direct_link: {api_resp.text[:500]}")
    print("Dobijen direktan CDN link, preuzimam fajl...")

    with session.get(direct_link, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        last_pct = -1
        with open(destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct and pct % 10 == 0:
                            print(f"Preuzeto: {pct}%")
                            last_pct = pct
    size_mb = os.path.getsize(destination) / (1024 * 1024)
    print(f"Preuzimanje zavrseno: {destination} (~{size_mb:.1f} MB)")


def get_duration_seconds(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=FFPROBE_TIMEOUT)
    return float(json.loads(result.stdout)["format"]["duration"])


def extract_audio(source_path, audio_path, duration_seconds):
    target_bitrate = max(24, min(64, int((23 * 8 * 1024) / duration_seconds)))
    print(f"Izdvajam audio pri {target_bitrate}kbps...")
    cmd = ["ffmpeg", "-y", "-i", source_path, "-vn", "-ac", "1", "-b:a", f"{target_bitrate}k", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
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


def snap_time_to_words(target, words, key):
    if not words:
        return target
    closest = min(words, key=lambda w: abs(w[key] - target))
    return closest[key]


def find_single_hook_segment(words, api_key, total_duration, content_criteria):
    # NAPOMENA: content_criteria opisuje OPSTE, trajne kriterijume sadrzaja
    # (vidi CONTENT_CRITERIA gore) - ne menja se po epizodi. Ako ova funkcija
    # ipak stalno vraca prazne clips-ove, to znaci da u konkretnoj epizodi
    # zaista ne postoji nijedan segment koji ispunjava ni jednu od tri
    # odobrene kategorije - retko, ali moguce (npr. cisto tehnicki deo bez
    # ijedne izdvojene licne/kontrarijanske/advice izjave).
    lines = [f"[{w['start']:.1f}] {w['word']}" for w in words]
    transcript_text = " ".join(lines)
    if len(transcript_text) > 60000:
        transcript_text = transcript_text[:60000]

    prompt = (
        "Ovo je transkript epizode biznis/VC podkasta 'Trailblazers' (gost: Ann Miura-Ko, VC "
        "investitorka - Floodgate), sa vremenskim oznakama u sekundama pre svake reci "
        "(format [12.3] rec).\n\n"
        f"{transcript_text}\n\n"
        f"{content_criteria}\n\n"
        "Dodatna pravila za sam klip (vaze bez obzira na kategoriju):\n"
        "- Prva izjava (hook) mora biti SNAZNA, KONKRETNA i pomalo SOKANTNA/PROVOKATIVNA - "
        "treba da izazove reakciju 'cekaj, sta?' ili 'wow' u prve 2 sekunde. Mora biti jasan "
        "STAV ili KONKRETNA TVRDNJA, ne mekana/neodredjena recenica.\n"
        "ZABRANJENO za hook: generalne, blage recenice tipa 'i think one is...', 'i think the "
        "leadership is...', 'so basically what happened is...' - ovakve recenice ne otkrivaju "
        "NISTA konkretno i NE SME biti prva recenica u klipu, cak i ako je to gde tema realno "
        "pocinje u transkriptu - u tom slucaju TRAZI DRUGI segment koji ima jaci, konkretniji "
        "pocetak, ili pomeri pocetak klipa unapred/unazad do prve stvarno jake recenice unutar "
        "iste teme.\n"
        "- Pocni na najjacoj/najkonkretnijoj recenici, NE na uvodu ili najavi ('welcome back to "
        "the show' stil je ZABRANJEN).\n"
        "- Svaka pojedinacna izjava mora poceti TACNO na pocetku recenice/izjave (ne usred reci "
        "ni usred nepovezane misli) i zavrsiti tacno na kraju te izjave - cist rez, bez 'mrtvog "
        "vazduha' pre ili posle, prva rec se NIKAD ne sme cuti isecena.\n"
        f"- Ukupno trajanje: izmedju {MIN_CLIP_SECONDS} i {MAX_CLIP_SECONDS} sekundi.\n"
        "- Samo JEDNA jasna ideja/lekcija/prica u ovom klipu (ne kombinuj sa drugim "
        "nepovezanim temama).\n"
        "- Koristi TACNE reci iz transkripta - ne parafraziraj i ne izmisljaj njene izjave.\n\n"
        "AKO stvarno NIJEDAN segment iz transkripta ne ispunjava nijednu od tri kategorije, "
        "odgovori sa praznim clips nizom (\"clips\": []) i u reason polju napisi zasto - "
        "NEMOJ izmisljati ili aproksimirati segmente koji ne ispunjavaju kriterijume.\n\n"
        "Napravi i JEDINSTVEN Instagram caption (na engleskom, 1-2 recenice) koji se konkretno "
        "odnosi na ovaj sadrzaj. Caption MORA da sadrzi SVE sledece: (1) eksplicitno pomene "
        "'Ann Miura-Ko' i 'Trailblazers' da bude jasno da je ovo intervju sa njom na tom "
        "podkastu, (2) tacnu recenicu 'Subscribe to @thetrailblazerspod on YouTube for the "
        "full episode', (3) odvojen tag '@trailblazers_pod', (4) 3-5 relevantnih hashtag-ova "
        "(npr #VC #Startups #AnnMiuraKo #Trailblazers #VentureCapital).\n\n"
        "Odgovori ISKLJUCIVO validnim JSON objektom, bez ikakvog dodatnog teksta:\n"
        '{"clips": [{"start": <broj>, "end": <broj>}, ...], '
        '"category": "<standalone_advice|hot_take|story_clip>", '
        '"reason": "<kratko objasnjenje na srpskom>", '
        '"caption": "<Instagram caption na engleskom>"}'
    )

    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}

    def do_call():
        return requests.post(url, headers=headers, json=payload, timeout=180)

    response = retry_request(do_call, "Claude hook-detekcija (novi reel)")
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
        print(f"[dijagnostika] Pun odgovor:\n{text}")
        raise RuntimeError("Claude nije vratio validan JSON za trazenu temu.") from e

    raw_clips = h.get("clips", [])
    print(f"[dijagnostika] Claude kategorija: {h.get('category', '(nema)')}")
    print(f"[dijagnostika] Claude reason: {h.get('reason', '(nema)')}")
    print(f"[dijagnostika] Claude je vratio {len(raw_clips)} sirovih clip segmenata: {raw_clips}")

    snapped_clips = []
    for c in raw_clips:
        try:
            start = max(0.0, float(c["start"]))
            end = min(float(c["end"]), total_duration)
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 1.0:
            continue
        # VAZNO: MAX_SINGLE_CLIP_SECONDS (18s) postoji da nijedna POJEDINACNA
        # izjava ne pojede ceo budzet kad se SPAJA VISE kratkih izjava u
        # supercut. Ako Claude vrati SAMO JEDNU izjavu (standalone advice /
        # story clip koji stoji sam za sebe), ne smemo je grubo odseci na
        # 18s - to bukvalno prekida recenicu u pola. U tom slucaju dozvoljavamo
        # joj da traje do MAX_CLIP_SECONDS (60s), sto je siguran fallback.
        if len(raw_clips) > 1:
            end = min(end, start + MAX_SINGLE_CLIP_SECONDS)
        else:
            end = min(end, start + MAX_CLIP_SECONDS)
        start = max(0.0, snap_time_to_words(start, words, "start") - 0.15)
        end = min(total_duration, snap_time_to_words(end, words, "end") + 0.15)
        if end - start >= 1.0:
            snapped_clips.append([round(start, 2), round(end, 2)])

    # Ako Claude ipak ne vrati nijedan validan segment (retko, sa opstim
    # kriterijumima), skripta se ne rusi - main() ovo obradjuje elegantno.
    if not snapped_clips:
        reason = h.get("reason", "(Claude nije naveo razlog)")
        print(f"[dijagnostika] Nijedan validan segment nije pronadjen. Reason: {reason}")
        return {"clips": [], "reason": reason, "caption": "", "category": h.get("category", "")}

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
        "category": h.get("category", ""),
    }


def build_supercut_with_transitions(source_path, clips, output_path, transition=TRANSITION_SECONDS):
    """Spaja pojedinacne izjave sa malim xfade (video) / acrossfade (audio)
    tranzicijama izmedju njih, umesto suvog reza - drzi paznju gledaoca."""
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
        # -r 30 -vsync cfr: NATERAJ tacno 30 konstantnih frejmova/sek. Bez ovoga
        # izvorni snimak (cesto blago nepravilnog broja frejmova/sek, uobicajeno
        # kod snimljenih poziva/podkasta) prenosi tu nepravilnost u iseceni klip,
        # sto zbunjuje Remotion renderer ("No frame found at position X").
        "-r", "30", "-vsync", "cfr",
        "-c:a", "aac", "-b:a", "192k", output_path,
    ]
    print(f"Spajam {n} izjava sa {transition}s tranzicijama...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
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
        print(f"{destination} vec postoji, preskacem preuzimanje.")
        return
    tmp = destination + ".partial"
    request = service.files().get_media(fileId=file_id)
    with open(tmp, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk(num_retries=5)
    os.rename(tmp, destination)


def render_with_remotion(video_path, words, duration_seconds, output_path, background_audio_path=None):
    """Renderuje kompletan finalni video kroz Remotion: animirani brending,
    animirani titlovi rec-po-rec, suptilni pozadinski akcenti, i mix
    dijaloga + pozadinske muzike.

    VAZNO: Remotion render NE MOZE da ucita fajl direktno preko apsolutne
    Windows putanju (npr. C:/Users/...) - mora preko svog lokalnog servera,
    koriscenjem staticFile() na TSX strani. Zato OVDE prvo kopiramo video i
    muziku u remotion/trailblazers/public/ folder, i prosledjujemo SAMO ime
    fajla (ne punu putanju) kao props - TrailblazersReel.tsx onda poziva
    staticFile(videoPath) da ih ucita ispravno.
    """
    public_dir = os.path.join(REMOTION_WORKDIR, "public")
    os.makedirs(public_dir, exist_ok=True)

    video_filename = "render_video.mp4"
    shutil.copyfile(video_path, os.path.join(public_dir, video_filename))

    bg_filename = ""
    if background_audio_path:
        bg_filename = "render_bg_audio.mp3"
        shutil.copyfile(background_audio_path, os.path.join(public_dir, bg_filename))

    props = {
        "videoPath": video_filename,
        "bgMusicPath": bg_filename,
        "bgMusicVolume": BACKGROUND_AUDIO_VOLUME,
        "durationInSeconds": duration_seconds,
        "words": [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in words],
    }
    props_path = "output_clips_newreel/remotion_props.json"
    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f)

    npx_cmd = shutil.which("npx") or "npx"
    cmd = [
        npx_cmd, "remotion", "render",
        REMOTION_ENTRY, REMOTION_COMPOSITION_ID, os.path.abspath(output_path),
        f"--props={os.path.abspath(props_path)}",
        "--log=verbose",
    ]
    print(f"Renderujem finalni video kroz Remotion (koristim: {npx_cmd})...")
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=REMOTION_TIMEOUT, cwd=REMOTION_WORKDIR,
    )
    if result.returncode != 0:
        print((result.stdout or "")[-3000:])
        print((result.stderr or "")[-3000:])
        raise RuntimeError("Remotion render nije uspeo.")
    print(f"Remotion render zavrsen: {output_path}")


def main():
    openai_key = os.environ["OPENAI_API_KEY"]
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]

    if not os.path.exists(SOURCE_PATH):
        download_from_wetransfer(WETRANSFER_SHORT_URL, SOURCE_PATH)
    else:
        print(f"{SOURCE_PATH} vec postoji, preskacem preuzimanje.")

    duration = get_duration_seconds(SOURCE_PATH)
    print(f"Trajanje izvornog videa: {duration:.1f}s")

    audio_path = "annmiura_audio_newreel.mp3"
    extract_audio(SOURCE_PATH, audio_path, duration)
    words = transcribe_audio(audio_path, openai_key)
    print(f"Transkripcija: {len(words)} reci.")

    with open("transcript_debug_newreel.json", "w", encoding="utf-8") as f:
        json.dump(words, f, indent=2)

    hook = find_single_hook_segment(words, anthropic_key, duration, CONTENT_CRITERIA)
    with open("hook_debug_newreel.json", "w", encoding="utf-8") as f:
        json.dump(hook, f, indent=2, ensure_ascii=False)

    clips = hook["clips"]

    # Graceful izlazak umesto pada builda kad nijedan segment ne ispunjava
    # kriterijume. Job se zavrsava USPESNO (exit code 0), samo bez
    # generisanog klipa - jasno se vidi u logu zasto.
    if not clips:
        print("=" * 70)
        print("NEMA REELA ZA OVU EPIZODU.")
        print("Nijedan segment iz transkripta ne ispunjava nijednu od tri")
        print("odobrene kategorije (standalone advice / hot-take / story clip).")
        print(f"Razlog (Claude): {hook['reason']}")
        print("Ovo NIJE greska u kodu - retko, ali moguce je da epizoda")
        print("jednostavno nema nijedan segment koji ispunjava kriterijume.")
        print("=" * 70)
        sys.exit(0)

    print(f"Izabrana kategorija: {hook.get('category', '(nepoznato)')}")
    print(f"Izabran segment: {len(clips)} izjava, {clips} - {hook['reason']}")

    os.makedirs("output_clips_newreel", exist_ok=True)
    supercut_path = "output_clips_newreel/supercut.mp4"
    final_path = "output_clips_newreel/final_newreel.mp4"

    build_supercut_with_transitions(SOURCE_PATH, clips, supercut_path)
    sc_duration = get_duration_seconds(supercut_path)
    print(f"Trajanje supercuta: {sc_duration:.1f}s")

    sc_audio = "output_clips_newreel/supercut_audio.mp3"
    extract_audio(supercut_path, sc_audio, sc_duration)
    sc_words = transcribe_audio(sc_audio, openai_key)

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
            print(f"Izabrana pozadinska muzika: {chosen_background_audio}")
        except Exception as e:
            print(f"Nisam uspeo da preuzmem pozadinsku muziku (nastavljam bez nje): {e}")
    else:
        print("GDRIVE_CREDENTIALS_JSON nije podesen - nastavljam bez pozadinske muzike.")

    render_with_remotion(
        supercut_path, sc_words, sc_duration, final_path, background_audio_path=chosen_background_audio
    )

    with open("output_clips_newreel/info.txt", "w", encoding="utf-8") as f:
        f.write(
            f"Kategorija: {hook.get('category', '')}\n\n"
            f"Reason: {hook['reason']}\n\nCaption: {hook['caption']}\n\n"
            f"Clips (sec): {clips}\n\nTrajanje finalnog klipa: {sc_duration:.1f}s\n"
        )

    print(f"NOVI REEL GOTOV (za pregled, NIJE automatski objavljen na Instagram): {final_path}")


if __name__ == "__main__":
    main()
