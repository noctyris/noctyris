#!/usr/bin/env python3
"""
HyperOS bootloader unlocker — daemon version.
- Boucle continue : une tentative chaque nuit a 00:00:00.000 (heure de Pekin)
- Heartbeat Telegram a T-2h chaque jour (22:00 UTC+8)
- Relance automatique en cas de crash
Prevu pour tourner en tache de fond (voir l'unite systemd fournie).
"""
import hashlib
import random
import time
import threading
import urllib3
import urllib.parse
import urllib.request
import json
from datetime import datetime, timezone, timedelta
import ntplib
import pytz

# ===== Telegram config =====
TELEGRAM_BOT_TOKEN = "8902584524:AAGuXSy1Lxbwz7Jt-3ohe4EbhrIXrHUHJ4w"
TELEGRAM_CHAT_ID = "8481215054"

# ===== Cookie =====
# Le token est lu depuis un fichier externe, il survit aux mises a jour du script.
# Chemin par defaut : /opt/hyperos/cookie.txt (contenu = la valeur de new_bbs_serviceToken)
# Pour tester en local : creez le fichier a cote du script, ou changez COOKIE_FILE.
import os
COOKIE_FILE = os.environ.get("HYPEROS_COOKIE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt"))

_cookie_cache = None

def get_cookie() -> str:
    """Lit le cookie depuis le fichier (une seule fois, puis cache)."""
    global _cookie_cache
    if _cookie_cache is None:
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                _cookie_cache = value
            else:
                log(f"[Cookie Error] Fichier vide : {COOKIE_FILE}")
                send_telegram("\u231B HyperOS unlocker : cookie.txt est vide.\nRemets la valeur de new_bbs_serviceToken dedans, sinon les tentatives echoueront.")
                _cookie_cache = ""
        except FileNotFoundError:
            log(f"[Cookie Error] Fichier introuvable : {COOKIE_FILE}")
            send_telegram("\u231B HyperOS unlocker : cookie.txt introuvable.\nCreer le fichier avec la valeur de new_bbs_serviceToken, sinon les tentatives echoueront.")
            _cookie_cache = ""
    return _cookie_cache

BEIJING_TZ = pytz.timezone("Asia/Shanghai")
API_HOST = "sgp-api.buy.mi.com"

ntp_servers = [
    "ntp0.ntp-servers.net", "ntp1.ntp-servers.net", "ntp2.ntp-servers.net",
    "ntp3.ntp-servers.net", "ntp4.ntp-servers.net", "ntp5.ntp-servers.net",
    "ntp6.ntp-servers.net"
]


def send_telegram(text: str):
    """Envoie une notification Telegram. Retourne le message_id ou None."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return body.get("result", {}).get("message_id")
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return None


def edit_telegram(message_id: int, text: str):
    """Edite un message Telegram existant (max 48 h apres l'envoi)."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text}).encode("utf-8")
        urllib.request.urlopen(urllib.request.Request(url, data=payload, method="POST"), timeout=10).read()
    except Exception as e:
        print(f"[Telegram Edit Error] {e}")


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def generate_device_id():
    random_data = f"{random.random()}-{time.time()}"
    return hashlib.sha1(random_data.encode('utf-8')).hexdigest().upper()


def get_initial_beijing_time():
    client = ntplib.NTPClient()
    for server in ntp_servers:
        try:
            log(f"NTP : synchro sur {server}...")
            response = client.request(server, version=3)
            ntp_time = datetime.fromtimestamp(response.tx_time, timezone.utc)
            beijing_time = ntp_time.astimezone(BEIJING_TZ)
            log(f"Heure de Pekin (NTP) : {beijing_time.strftime('%Y-%m-%d %H:%M:%S.%f')}")
            return beijing_time
        except Exception as e:
            log(f"NTP {server} echec: {e}")
    return None


def get_synchronized_beijing_time(start_beijing_time, start_timestamp):
    elapsed = time.time() - start_timestamp
    return start_beijing_time + timedelta(seconds=elapsed)


def wait_until_target_time(start_beijing_time, start_timestamp):
    next_day = start_beijing_time + timedelta(days=1)
    # Tir pile a 00:00:00.000 heure de Pekin (le serveur horodate a la reception)
    target_time = next_day.replace(hour=0, minute=0, second=0, microsecond=0)
    log(f"Attente jusqu'a {target_time.strftime('%Y-%m-%d %H:%M:%S.%f')} (UTC+8)")
    prewarm_done = False
    pre_conn = None
    while True:
        current_time = get_synchronized_beijing_time(start_beijing_time, start_timestamp)
        time_diff = target_time - current_time
        if time_diff.total_seconds() > 1:
            time.sleep(min(1.0, time_diff.total_seconds() - 1))
        # Pre-warm dans les 40 dernieres secondes seulement (pas des heures avant :
        # une connexion TLS ouverte trop tot est fermee par le serveur ou le NAT)
        elif time_diff.total_seconds() <= 40 and not prewarm_done:
            prewarm_done = True
            try:
                import http.client
                conn = http.client.HTTPSConnection(API_HOST, timeout=15)
                conn.request("GET", "/bbs/api/global/user/bl-switch/state", headers={
                    "Cookie": f"new_bbs_serviceToken={get_cookie()};versionCode=500411;versionName=5.4.11;deviceId={generate_device_id()};",
                    "User-Agent": "okhttp/4.12.0",
                    "Connection": "keep-alive",
                })
                conn.getresponse().read()
                log("TLS pre-chauffee (T-40s), connexion keep-alive etablie")
                pre_conn = conn
            except Exception as e:
                log(f"[Pre-warm Error] {e} - tir sur pool standard")
        elif current_time >= target_time:
            log(f"C'est l'heure : {current_time.strftime('%Y-%m-%d %H:%M:%S.%f')}")
            return target_time, pre_conn
        else:
            time.sleep(0.0001)


def check_unlock_status(session, device_id):
    """Retourne (ok, message). Ne bloque jamais sur input() (mode daemon)."""
    try:
        url = f"https://{API_HOST}/bbs/api/global/user/bl-switch/state"
        headers = {
            "Cookie": f"new_bbs_serviceToken={get_cookie()};versionCode=500411;versionName=5.4.11;deviceId={device_id};"
        }
        response = session.make_request('GET', url, headers=headers)
        if response is None:
            return False, "impossible de recuperer le statut"

        response_data = json.loads(response.data.decode('utf-8'))
        response.release_conn()

        if response_data.get("code") == 100004:
            return False, "EXPIRED_COOKIE"

        data = response_data.get("data", {})
        is_pass = data.get("is_pass")
        button_state = data.get("button_state")
        deadline_format = data.get("deadline_format", "")

        if is_pass == 4:
            if button_state == 1:
                return True, "OK : les requetes seront envoyees"
            elif button_state == 2:
                return True, f"Requetes bloquees jusqu'au {deadline_format} (M/D) - envoi tente quand meme"
            elif button_state == 3:
                return True, "Compte cree il y a moins de 30 jours - envoi tente quand meme"
        elif is_pass == 1:
            return False, "ALREADY_UNLOCKED:" + str(deadline_format)
        return True, f"Statut inconnu (is_pass={is_pass}, button_state={button_state})"
    except Exception as e:
        return False, f"erreur au check de statut : {e}"


class HTTP11Session:
    def __init__(self):
        self.http = urllib3.PoolManager(
            maxsize=10,
            retries=True,
            timeout=urllib3.Timeout(connect=2.0, read=15.0),
            headers={}
        )

    def make_request(self, method, url, headers=None, body=None):
        try:
            request_headers = {}
            if headers:
                request_headers.update(headers)
                request_headers['Content-Type'] = 'application/json; charset=utf-8'
            if method == 'POST':
                if body is None:
                    body = '{"is_retry":true}'.encode('utf-8')
                request_headers['Content-Length'] = str(len(body))
                request_headers['Accept-Encoding'] = 'gzip, deflate, br'
                request_headers['User-Agent'] = 'okhttp/4.12.0'
                request_headers['Connection'] = 'keep-alive'
            response = self.http.request(method, url, headers=request_headers, body=body, preload_content=False)
            return response
        except Exception as e:
            log(f"[Network Error] {e}")
            return None


def daily_attempt():
    """Une tentative complete. Retourne 'done', 'ok' ou 'expired'."""
    device_id = generate_device_id()
    session = HTTP11Session()

    ok, status_msg = check_unlock_status(session, device_id)
    log(f"Statut compte : {status_msg}")
    if not ok:
        if status_msg.startswith("ALREADY_UNLOCKED"):
            send_telegram(f"\U0001F513 HyperOS unlock : compte deja debloque ({status_msg.split(':', 1)[1]}).\nLe daemon s'arrete.")
            return "done"
        if status_msg == "EXPIRED_COOKIE":
            send_telegram("\u231B HyperOS unlocker : cookie new_bbs_serviceToken expire !\nMets-le a jour dans le script, sinon les tentatives echoueront.")
            return "expired"
        send_telegram(f"\u26A0\uFE0F HyperOS unlocker : {status_msg}. Nouvelle tentative demain.")
        return "ok"

    start_beijing_time = get_initial_beijing_time()
    if start_beijing_time is None:
        send_telegram("\u26A0\uFE0F HyperOS unlocker : aucun serveur NTP joignable, tentative annulee pour aujourd'hui.")
        return "ok"

    start_timestamp = time.time()
    prewarm_used = False
    _, pre_conn = wait_until_target_time(start_beijing_time, start_timestamp)

    url = f"https://{API_HOST}/bbs/api/global/apply/bl-auth"
    headers = {
        "Cookie": f"new_bbs_serviceToken={get_cookie()};versionCode=500411;versionName=5.4.11;deviceId={device_id};"
    }

    while True:
        request_time = get_synchronized_beijing_time(start_beijing_time, start_timestamp)
        log(f"Requete envoyee a {request_time.strftime('%H:%M:%S.%f')} (UTC+8)")

        response = None
        if pre_conn is not None:
            try:
                body = b'{"is_retry":true}'
                pre_conn.request("POST", "/bbs/api/global/apply/bl-auth", body=body, headers={
                    "Cookie": headers["Cookie"],
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "Accept-Encoding": "gzip, deflate, br",
                    "User-Agent": "okhttp/4.12.0",
                    "Connection": "keep-alive",
                })
                resp = pre_conn.getresponse()
                raw = resp.read()
                class _R:
                    data = raw
                response = _R()
                resp.close()
                pre_conn.close()
                pre_conn = None
                prewarm_used = True
            except Exception as e:
                log(f"[Pre-warmed Request Error] {e} - repli sur le pool")
                response = None

        if response is None:
            response = session.make_request('POST', url, headers=headers)
        if response is None:
            continue

        response_time = get_synchronized_beijing_time(start_beijing_time, start_timestamp)
        latency_ms = int((response_time - request_time).total_seconds() * 1000)
        via = "connexion pre-chauffee" if prewarm_used else "pool standard"
        log(f"Reponse recue a {response_time.strftime('%H:%M:%S.%f')} (latence {latency_ms} ms, {via})")

        try:
            response_data = response.data
            try:
                response.release_conn()
            except Exception:
                pass
            json_response = json.loads(response_data.decode('utf-8'))
            code = json_response.get("code")
            data = json_response.get("data", {})

            result_note = f"Reponse brute : {json_response}"
            finished = False
            if code == 0:
                apply_result = data.get("apply_result")
                if apply_result == 1:
                    result_note = "\u2705 Demande approuvee ! Verification du statut..."
                    ok, status_msg = check_unlock_status(session, device_id)
                    result_note += f"\n\U0001F4CB Statut : {status_msg}"
                    finished = True
                elif apply_result == 3:
                    result_note = f"\u26D5 Quota atteint, nouvel essai le {data.get('deadline_format', '?')} (M/D)"
                    finished = True
                elif apply_result == 4:
                    result_note = f"\U0001F6AB Compte bloque jusqu'au {data.get('deadline_format', '?')} (M/D)"
                    finished = True
            elif code == 100001:
                result_note = f"\u274C Requete rejetee : {json_response}"
            elif code == 100003:
                result_note = f"\u2753 Possiblement approuvee : {json_response}"
                ok, status_msg = check_unlock_status(session, device_id)
                result_note += f"\n\U0001F4CB Statut : {status_msg}"
            elif code == 100004:
                result_note = "\u231B Cookie expire - mets a jour cookie.txt sur la machine"
                finished = True

            send_telegram(
                "\U0001F680 HyperOS unlock - tentative du jour\n"
                f"\u23F1\uFE0F Requete envoyee : {request_time.strftime('%H:%M:%S.%f')} (UTC+8)\n"
                f"\U0001F4E9 Reponse recue : {response_time.strftime('%H:%M:%S.%f')} (UTC+8)\n"
                f"\U0001F4E1 Latence : {latency_ms} ms ({via})\n"
                f"\U0001F5A5\uFE0F Serveur : {API_HOST}\n"
                f"\U0001F4CA Code : {code}\n"
                f"{result_note}"
            )
            if finished:
                if code == 100004:
                    return "expired"
                return "done" if code == 0 and data.get("apply_result") == 1 else "ok"

        except json.JSONDecodeError:
            log(f"[Error] Reponse illisible : {response_data[:200]}")
            send_telegram(
                f"\u26A0\uFE0F HyperOS unlock - reponse illisible ({response_time.strftime('%H:%M:%S.%f')} UTC+8, {latency_ms} ms)\n"
                f"Reponse brute : {response_data[:200]}"
            )
            return "ok"
        except Exception as e:
            log(f"[Error response processing] {e}")
            continue


def heartbeat_loop():
    """Envoie un heartbeat a 22:00 (T-2h) puis l'edite toutes les 20 min
    jusqu'a minuit, pour eviter de polluer le chat tout en prouvant la vie."""
    while True:
        now = datetime.now(BEIJING_TZ)
        next_beat = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if next_beat <= now:
            next_beat += timedelta(days=1)
        sleep_s = (next_beat - now).total_seconds()
        log(f"Heartbeat prochain a {next_beat.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8), dans {sleep_s/3600:.1f} h")
        time.sleep(sleep_s)

        def beat_text():
            now = datetime.now(BEIJING_TZ)
            target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour == 23:
                target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            rem_s = max(0, int((target - now).total_seconds()))
            h, rem = divmod(rem_s, 3600)
            m, s = divmod(rem, 60)
            return (f"\U0001F493 HyperOS unlocker en vie\n"
                    f"Heure de Pekin : {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Tentative dans {h:02d}:{m:02d}:{s:02d}")

        msg_id = send_telegram(beat_text())
        if msg_id is None:
            continue
        # Edition toutes les 20 min jusqu'a 00:00 (6 editions entre 22:00 et 23:40)
        for _ in range(6):
            time.sleep(20 * 60)
            if datetime.now(BEIJING_TZ).hour >= 23:
                break
            edit_telegram(msg_id, beat_text())


def daemon_loop():
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    while True:
        try:
            result = daily_attempt()
            if result == "done":
                log("Compte debloque - arret du daemon.")
                return
            if result == "expired":
                # Cookie expire : reessaie dans 6 h (le temps que tu le mettes a jour)
                log("Cookie expire, nouvelle verification dans 6 h.")
                time.sleep(6 * 3600)
                continue
        except Exception as e:
            log(f"[Loop Error] {e}")
            try:
                send_telegram(f"\u26A0\uFE0F HyperOS unlocker : crash, relance dans 60 s.\nErreur : {e}")
            except Exception:
                pass
            time.sleep(60)
            continue
        # Dormir jusqu'au prochain minuit Pekin + 1 h de marge, puis reboucler
        now = datetime.now(BEIJING_TZ)
        next_run = (now + timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
        log(f"Prochaine tentative dans {(next_run - now).total_seconds()/3600:.1f} h")
        time.sleep(max(0, (next_run - now).total_seconds()))


if __name__ == "__main__":
    log("=== HyperOS Unlocker Daemon demarre ===")
    send_telegram("\U0001F7E2 HyperOS unlocker daemon demarre")
    daemon_loop()
