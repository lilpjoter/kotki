"""Kotki uploader: playlista animacji 24x8 dla ESP8266 + MAX7219.

Przegladarka: wgrywa GIF-y/obrazki, serwer zamienia je na klatki 24x8.
ESP: pyta o /api/version i gdy sie zmieni, pobiera /api/playlist.bin.
"""
import hashlib
import hmac
import io
import json
import os
import struct
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, send_from_directory
from PIL import Image, ImageChops, ImageOps, ImageSequence

W, H = 24, 8
MAX_ANIMS = 16            # tyle miesci indeks w szkicu ESP
MAX_FRAMES = 200          # na jedna animacje
MAX_TOTAL_FRAMES = 1500   # razem (1500 * 24 B = 36 kB na flashu ESP)
MAX_UPLOAD = 8 * 1024 * 1024

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(BASE, "data", "playlist.json"))
PASSWORD = os.environ.get("UPLOAD_PASSWORD", "")

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

lock = threading.Lock()
state = {
    "animations": [],
    "pristine": True,      # True = playlista to nadal domyslne kotki
    "esp_seen": 0.0,
    # ustawienia wyswietlacza; "set" = ktos je zmienil od startu serwera
    "settings": {"brightness": 2, "solo": None, "set": False},
}


# ---------- przechowywanie ----------
def load():
    try:
        with open(DATA_FILE) as f:
            saved = json.load(f)
        state["pristine"] = bool(saved.get("pristine", False))
        if not state["pristine"]:
            state["animations"] = saved["animations"]
        st = saved.get("settings") or {}
        state["settings"] = {
            "brightness": max(0, min(15, int(st.get("brightness", 2)))),
            "solo": st.get("solo"),
            "set": bool(st.get("set", False)),
        }
    except (OSError, ValueError, KeyError, TypeError):
        state["pristine"] = True
    if state["pristine"]:
        with open(os.path.join(BASE, "seed.json")) as f:
            state["animations"] = json.load(f)


def save(playlist_changed=True):
    if playlist_changed:
        state["pristine"] = False
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"animations": state["animations"], "pristine": state["pristine"],
                   "settings": state["settings"]}, f)
    os.replace(tmp, DATA_FILE)


def build_bin():
    """KOT1 | u8 ile | na animacje: u16 klatki, u16 ms, u8 powtorzenia, klatki*24 B (little-endian)."""
    out = io.BytesIO()
    anims = state["animations"]
    out.write(b"KOT1")
    out.write(struct.pack("<B", len(anims)))
    for a in anims:
        out.write(struct.pack("<HHB", len(a["frames"]), a["ms"], a["repeat"]))
        for fr in a["frames"]:
            out.write(bytes.fromhex(fr))
    return out.getvalue()


def version():
    # "seed" = serwer swiezo po restarcie, nikt nic nie zmienil. ESP wtedy
    # nie nadpisuje tego, co ma juz zapisane u siebie.
    if state["pristine"]:
        return "seed"
    return hashlib.sha1(build_bin()).hexdigest()[:12]


def solo_index():
    sid = state["settings"]["solo"]
    for i, a in enumerate(state["animations"]):
        if a["id"] == sid:
            return i
    return -1


def esp_state():
    """Jedna linia dla ESP: "<wersja playlisty> <jasnosc> <solo>".
    Jasnosc 0-15, solo = numer animacji granej w petli albo -1 (cala playlista).
    "- -" = nikt nie ruszal ustawien od restartu serwera, ESP zostaje przy swoich."""
    st = state["settings"]
    if not st["set"]:
        return f"{version()} - -"
    return f"{version()} {st['brightness']} {solo_index()}"


# ---------- walidacja ----------
class Bad(Exception):
    pass


@app.errorhandler(Bad)
def bad(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(413)
def too_big(_e):
    return jsonify(error="Plik jest za duży (limit 8 MB)."), 413


def need_auth():
    if not PASSWORD:
        return None
    given = request.headers.get("X-Password", "")
    if hmac.compare_digest(given.encode(), PASSWORD.encode()):
        return None
    return jsonify(error="Złe hasło."), 401


def clean_int(v, lo, hi, what):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise Bad(f"{what}: to musi być liczba.")
    if not lo <= v <= hi:
        raise Bad(f"{what}: dozwolony zakres {lo}-{hi}.")
    return v


def clean_frames(frames):
    if not isinstance(frames, list) or not frames:
        raise Bad("Brak klatek.")
    if len(frames) > MAX_FRAMES:
        raise Bad(f"Za dużo klatek (limit {MAX_FRAMES} na animację).")
    out = []
    for fr in frames:
        if not isinstance(fr, str) or len(fr) != W * 2:
            raise Bad("Zły format klatki.")
        try:
            bytes.fromhex(fr)
        except ValueError:
            raise Bad("Zły format klatki.")
        out.append(fr.upper())
    return out


def clean_anim(d, keep_id=False):
    if not isinstance(d, dict):
        raise Bad("Zły format animacji.")
    name = str(d.get("name") or "Animacja").strip()[:40] or "Animacja"
    old_id = d.get("id")
    keep = keep_id and isinstance(old_id, str) and 1 <= len(old_id) <= 16 and old_id.isalnum()
    return {
        "id": old_id if keep else uuid.uuid4().hex[:8],
        "name": name,
        "ms": clean_int(d.get("ms", 150), 20, 5000, "Czas klatki"),
        "repeat": clean_int(d.get("repeat", 1), 1, 50, "Powtórzenia"),
        "frames": clean_frames(d.get("frames")),
    }


def check_limits(anims):
    if len(anims) > MAX_ANIMS:
        raise Bad(f"Limit to {MAX_ANIMS} animacji na playliście.")
    if sum(len(a["frames"]) for a in anims) > MAX_TOTAL_FRAMES:
        raise Bad(f"Playlista jest za długa (limit {MAX_TOTAL_FRAMES} klatek razem).")


# ---------- konwersja obrazka ----------
def to_frames(img, threshold, invert, fit):
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        durations.append(frame.info.get("duration", 0) or 0)
        rgba = frame.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))   # przezroczyste = zgaszone
        r, gr, b = Image.alpha_composite(bg, rgba).convert("RGB").split()
        g = ImageChops.lighter(ImageChops.lighter(r, gr), b)   # jasnosc = najjasniejszy kanal
        if g.size != (W, H):
            if fit == "stretch":
                g = g.resize((W, H), Image.LANCZOS)
            elif fit == "cover":
                g = ImageOps.fit(g, (W, H), Image.LANCZOS)
            else:  # contain
                g = ImageOps.pad(g, (W, H), Image.LANCZOS, color=0)
        px = g.load()
        cols = []
        for x in range(W):
            b = 0
            for y in range(H):
                on = px[x, y] >= threshold
                if on != invert:
                    b |= 1 << y        # bit 0 = gorny wiersz
            cols.append(b)
        frames.append(bytes(cols).hex().upper())
        if len(frames) > MAX_FRAMES:
            raise Bad(f"Ten plik ma ponad {MAX_FRAMES} klatek. Skróć animację.")
    return spread(frames, durations)


def spread(frames, durations):
    """GIF ma czas osobno dla kazdej klatki, a ESP jeden czas na animacje.
    Bierzemy najkrotszy czas jako podstawe i powtarzamy klatki, ktore trwaja dluzej."""
    real = [d for d in durations if d > 0]
    if len(frames) == 1 or not real:
        return frames, (1000 if len(frames) == 1 else 150)
    base = max(20, min(real))
    durations = [d if d > 0 else base for d in durations]
    while True:
        counts = [max(1, round(d / base)) for d in durations]
        if sum(counts) <= MAX_FRAMES:
            break
        base *= 2
    out = []
    for fr, n in zip(frames, counts):
        out.extend([fr] * n)
    return out, min(5000, base)


# ---------- strony ----------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


# ---------- API dla ESP ----------
@app.get("/api/version")
def api_version():
    with lock:
        if request.args.get("d") == "esp":
            state["esp_seen"] = time.time()
        return Response(version(), mimetype="text/plain")


@app.get("/api/state")
def api_state():
    with lock:
        if request.args.get("d") == "esp":
            state["esp_seen"] = time.time()
        return Response(esp_state(), mimetype="text/plain")


@app.get("/api/playlist.bin")
def api_bin():
    with lock:
        return Response(build_bin(), mimetype="application/octet-stream")


# ---------- API dla przegladarki ----------
def playlist_json():
    seen = state["esp_seen"]
    return jsonify(
        version=version(),
        pristine=state["pristine"],
        password_required=bool(PASSWORD),
        esp_seconds_ago=(round(time.time() - seen) if seen else None),
        limits={"anims": MAX_ANIMS, "frames": MAX_FRAMES, "total": MAX_TOTAL_FRAMES},
        brightness=state["settings"]["brightness"],
        solo=(state["settings"]["solo"] if solo_index() >= 0 else None),
        animations=state["animations"],
    )


@app.get("/api/playlist")
def api_playlist():
    with lock:
        return playlist_json()


@app.post("/api/convert")
def api_convert():
    denied = need_auth()
    if denied:
        return denied
    f = request.files.get("file")
    if not f:
        raise Bad("Nie wybrano pliku.")
    threshold = clean_int(request.form.get("threshold", 128), 1, 255, "Próg")
    invert = request.form.get("invert") == "1"
    fit = request.form.get("fit", "contain")
    try:
        img = Image.open(io.BytesIO(f.read()))
        frames, ms = to_frames(img, threshold, invert, fit)
    except Bad:
        raise
    except Exception:
        raise Bad("Nie udało się odczytać pliku. Obsługiwane: GIF, PNG, JPG, BMP, WebP.")
    return jsonify(frames=frames, ms=ms)


@app.post("/api/animations")
def api_add():
    denied = need_auth()
    if denied:
        return denied
    anim = clean_anim(request.get_json(silent=True))
    with lock:
        check_limits(state["animations"] + [anim])
        state["animations"].append(anim)
        save()
        return playlist_json()


@app.put("/api/playlist")
def api_replace():
    """Podmiana calej playlisty (import / przywracanie kopii z przegladarki)."""
    denied = need_auth()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    items = data.get("animations")
    if not isinstance(items, list):
        raise Bad("Zły format playlisty.")
    anims = [clean_anim(a, keep_id=True) for a in items]
    if len({a["id"] for a in anims}) != len(anims):
        raise Bad("Zły format playlisty (powtórzone id).")
    check_limits(anims)
    with lock:
        state["animations"] = anims
        save()
        return playlist_json()


@app.patch("/api/animations/<aid>")
def api_edit(aid):
    denied = need_auth()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with lock:
        for a in state["animations"]:
            if a["id"] == aid:
                if "name" in data:
                    a["name"] = str(data["name"]).strip()[:40] or a["name"]
                if "ms" in data:
                    a["ms"] = clean_int(data["ms"], 20, 5000, "Czas klatki")
                if "repeat" in data:
                    a["repeat"] = clean_int(data["repeat"], 1, 50, "Powtórzenia")
                if "frames" in data:
                    old = a["frames"]
                    a["frames"] = clean_frames(data["frames"])
                    try:
                        check_limits(state["animations"])
                    except Bad:
                        a["frames"] = old
                        raise
                save()
                return playlist_json()
    return jsonify(error="Nie ma takiej animacji."), 404


@app.delete("/api/animations/<aid>")
def api_delete(aid):
    denied = need_auth()
    if denied:
        return denied
    with lock:
        state["animations"] = [a for a in state["animations"] if a["id"] != aid]
        save()
        return playlist_json()


@app.put("/api/settings")
def api_settings():
    denied = need_auth()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    with lock:
        st = state["settings"]
        if "brightness" in data:
            st["brightness"] = clean_int(data["brightness"], 0, 15, "Jasność")
        if "solo" in data:
            sid = data["solo"]
            if sid is not None and sid not in [a["id"] for a in state["animations"]]:
                raise Bad("Nie ma takiej animacji.")
            st["solo"] = sid
        st["set"] = True
        save(playlist_changed=False)
        return playlist_json()


@app.post("/api/order")
def api_order():
    denied = need_auth()
    if denied:
        return denied
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    with lock:
        by_id = {a["id"]: a for a in state["animations"]}
        if sorted(ids) != sorted(by_id):
            raise Bad("Lista się zmieniła, odśwież stronę.")
        state["animations"] = [by_id[i] for i in ids]
        save()
        return playlist_json()


load()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
