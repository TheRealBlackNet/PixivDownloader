#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bild-Downloader (JDownloader-Stil) - Backend + HTML-Oberflaeche in EINER Datei.

Installation (einmalig):
    pip install playwright
    playwright install chromium        # nicht noetig, wenn Google Chrome installiert ist

Start:
    python bilder_downloader.py        # oeffnet http://127.0.0.1:8765 im Browser

Ablauf:
  1. Seiten-Links (pixiv-Werkseiten) in die GUI einfuegen.
  2. Ein Playwright-Browser (eigenes Profil, merkt sich den Login) laedt die Seite und
     liest per XPath (a) den href des ersten Bildes und (b) die Anzeige "1/N".
     Fehlt die Anzeige, ist es ein Einzelbild.
  3. Aus ..._p0.jpg werden ..._p1.jpg ... ..._p(N-1).jpg erzeugt und heruntergeladen
     (mit Referer, Fortsetzen per Range, Wiederholung mit Wartezeit, Limit parallel).
  4. Links, Status und Einstellungen werden in ./data gespeichert und beim Start geladen.
"""
import atexit
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "links.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
PROFILE_DIR = DATA_DIR / "browser_profile"

HOST = "127.0.0.1"
PORT = 8765
REFERER = "https://www.pixiv.net/"
LOGIN_URL = "https://accounts.pixiv.net/login"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BACKOFF = [3, 10, 30, 60, 120]          # Sekunden zwischen Wiederholungen

DEFAULTS = {
    "download_dir": "",                  # leer = Windows-Ordner "Downloads"
    "max_parallel": 3,                   # gleichzeitige Downloads
    "max_retries": 4,                    # Wiederholungen pro Bild
    "headless": True,                    # False = Browserfenster beim Aufloesen sichtbar
    "xpath_count": "/html/body/div[1]/div/div[2]/div[5]/div[1]/div/div[1]/main/section/div[1]/div/figure/div/div[1]/div/div/div/div/div/span",
    "xpath_link": "/html/body/div[1]/div/div[2]/div[5]/div[1]/div/div[1]/main/section/div[1]/div/figure/div/div[2]/div/a",
    # Alternative, falls der erste XPath nichts findet:
    "xpath_link_alt": "/html/body/div[1]/div/div[2]/div[5]/div[1]/div/div[1]/main/section/div[1]/div/figure/div[1]/div[1]/div/a",
    # Letzter Fallback (CSS): darf nur einmal vorkommen (oder genau einmal das Bild <img> enthalten)
    "css_link_fallback": "[href*=\"_p0.\"]",
}

STOP = threading.Event()
PROGRESS = {}                            # image_id -> (bytes_done, bytes_total)
UI = {"message": "", "login_open": False}
SETTINGS = None
db = None
RESOLVER = None


# ----------------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------------
def default_downloads_dir():
    """Windows: bekannter Ordner 'Downloads' (auch bei verschobenem Ordner), sonst ~/Downloads."""
    if os.name == "nt":
        try:
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                             (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            buf = ctypes.c_wchar_p()
            res = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buf))
            if res == 0 and buf.value:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:
            pass
    return str(Path.home() / "Downloads")


IMG_RE = re.compile(r"^(?P<pre>.*_p)(?P<n>\d+)(?P<ext>\.[A-Za-z0-9]+)(?P<q>\?.*)?$")
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def build_image_list(first_href, total):
    """Aus dem Link des ersten Bildes (..._p0.jpg) die Links aller Bilder (_p0 ... _p<N-1>) bauen."""
    m = IMG_RE.match(first_href)
    if total <= 1 or not m:
        return [first_href]
    return [f"{m['pre']}{i}{m['ext']}{m['q'] or ''}" for i in range(total)]


def file_name_from_url(url):
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "bild"
    return INVALID_NAME_CHARS.sub("_", name)


def candidate_urls(url):
    """Original-URL zuerst; bei 404 andere Endungen probieren (pixiv mischt jpg/png je Seite)."""
    yield url
    m = re.match(r"^(.*)\.([A-Za-z0-9]+)(\?.*)?$", url)
    if m:
        base, ext, q = m.group(1), m.group(2).lower(), m.group(3) or ""
        for alt in ("jpg", "png", "gif"):
            if alt != ext:
                yield f"{base}.{alt}{q}"


def is_direct_image(url):
    p = urlparse(url)
    return p.netloc.lower().endswith("pximg.net") or \
        p.path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


# ----------------------------------------------------------------------------
# Einstellungen + Datenbank
# ----------------------------------------------------------------------------
class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(SETTINGS_PATH.read_text("utf-8")))
        except Exception:
            pass
        if not str(self.data.get("download_dir") or "").strip():
            self.data["download_dir"] = default_downloads_dir()

    def get(self, key):
        with self.lock:
            return self.data[key]

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
        self.save()

    def save(self):
        with self.lock:
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, SETTINGS_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
    id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'wartend',
    total INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0, error TEXT, added REAL);
CREATE TABLE IF NOT EXISTS images(
    id INTEGER PRIMARY KEY, page_id INTEGER, url TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'wartend', bytes INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0, error TEXT, path TEXT, added REAL);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # Beim Start: unterbrochene Arbeiten wieder in die Warteschlange (.part-Dateien werden fortgesetzt)
            self.conn.execute("UPDATE pages SET status='wartend' WHERE status='aufloesen'")
            self.conn.execute("UPDATE images SET status='wartend', not_before=0 WHERE status='laedt'")
            self.conn.commit()

    def q(self, sql, args=(), one=False):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.fetchone() if one else cur.fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def insert_image(self, page_id, url):
        return self.x("INSERT OR IGNORE INTO images(page_id,url,name,status,added) VALUES(?,?,?,'wartend',?)",
                      (page_id, url, file_name_from_url(url), time.time()))


# ----------------------------------------------------------------------------
# Seiten aufloesen (Playwright, eigener Thread)
# ----------------------------------------------------------------------------
# Reihenfolge: 1. XPath, 2. Alternativ-XPath, 3. Fallback document.querySelector('[href*="_p0."]')
# (Fallback nur, wenn es genau EIN Element gibt; bei mehreren zaehlt nur das, das ein <img> enthaelt).
FIND_LINK_JS = """([xp1, xp2, css]) => {
  const byX = x => { try { return document.evaluate(x, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; } catch (e) { return null; } };
  const names = ["xpath", "xpath_alt"];
  const xs = [xp1, xp2];
  for (let i = 0; i < 2; i++) {
    const el = byX(xs[i]);
    const href = el && el.getAttribute && el.getAttribute("href");
    if (href) return {href: href, source: names[i]};
  }
  let list = [];
  try { list = Array.from(document.querySelectorAll(css)); } catch (e) {}
  if (list.length > 1) list = list.filter(e => e.querySelector("img"));
  if (list.length === 1) {
    const href = list[0].getAttribute("href");
    if (href) return {href: href, source: "fallback"};
  }
  return null;
}"""

DIAGNOSE_JS = """([xp1, xp2, css]) => {
  const n = x => { try { return document.evaluate(x, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength; } catch (e) { return -1; } };
  let c = -1; try { c = document.querySelectorAll(css).length; } catch (e) {}
  return {x1: n(xp1), x2: n(xp2), css: c, title: document.title};
}"""


class LinkNotFound(Exception):
    pass


class Resolver(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="resolver")
        self.cmds = queue.Queue()
        self.pw = None
        self.ctx = None
        self.init_error = ""
        self.PWTimeout = TimeoutError

    # --- Browser -----------------------------------------------------------
    def open_ctx(self, headless):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        last = None
        for channel in ("chrome", None):        # installiertes Chrome zuerst, sonst Playwright-Chromium
            try:
                kw = dict(user_data_dir=str(PROFILE_DIR), headless=headless, args=args)
                if channel:
                    kw["channel"] = channel
                return self.pw.chromium.launch_persistent_context(**kw)
            except Exception as e:                # noqa: BLE001
                last = e
        raise last

    def close_ctx(self):
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
            self.ctx = None

    # --- Login -------------------------------------------------------------
    def do_login(self):
        self.close_ctx()
        ctx = None
        try:
            ctx = self.open_ctx(False)
            UI["login_open"] = True
            UI["message"] = "Anmelde-Fenster ist offen: bei pixiv einloggen und das Fenster danach schliessen."
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(LOGIN_URL)
            while not STOP.is_set():
                if not ctx.pages:
                    break
                try:
                    ctx.pages[0].wait_for_timeout(500)
                except Exception:
                    break
        except Exception as e:                    # noqa: BLE001
            UI["message"] = f"Anmelde-Fenster konnte nicht geoeffnet werden: {e}"
        else:
            UI["message"] = ""
        finally:
            UI["login_open"] = False
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass

    # --- Seite aufloesen ---------------------------------------------------
    def resolve_page(self, row):
        pid, url = row["id"], row["url"]
        db.x("UPDATE pages SET status='aufloesen', error=NULL WHERE id=?", (pid,))
        if self.init_error:
            db.x("UPDATE pages SET status='fehler', error=? WHERE id=?", (self.init_error, pid))
            return
        page = None
        try:
            if self.ctx is None:
                self.ctx = self.open_ctx(bool(SETTINGS.get("headless")))
            page = self.ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            count_sel = "xpath=" + SETTINGS.get("xpath_count")
            args = [SETTINGS.get("xpath_link"), SETTINGS.get("xpath_link_alt"), SETTINGS.get("css_link_fallback")]
            try:
                found = page.wait_for_function(FIND_LINK_JS, arg=args, timeout=25000, polling=250).json_value()
            except self.PWTimeout:
                try:
                    d = page.evaluate(DIAGNOSE_JS, args)
                    raise LinkNotFound(
                        f"Bild-Link nicht gefunden - XPath 1: {d['x1']} Treffer, XPath 2: {d['x2']} Treffer, "
                        f"Fallback: {d['css']} Treffer (Seitentitel: {d['title']}). "
                        "Nicht angemeldet, Seite geloescht oder Layout geaendert?")
                except LinkNotFound:
                    raise
                except Exception:
                    raise LinkNotFound("Bild-Link nicht gefunden (nicht angemeldet, Seite geloescht "
                                       "oder Layout geaendert?)")
            href = urljoin(url, found["href"])
            source = found["source"]

            total = 1                               # keine Anzeige = Seite mit nur einem Bild
            try:
                page.wait_for_selector(count_sel, state="attached", timeout=4000)
                text = page.text_content(count_sel) or ""
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if m:
                    total = max(1, int(m.group(2)))
            except self.PWTimeout:
                pass

            urls = build_image_list(href, total)
            notes = []
            if source == "xpath_alt":
                notes.append("Link ueber Alternativ-XPath gefunden")
            elif source == "fallback":
                notes.append("Link ueber Fallback [href*=_p0.] gefunden")
            if total > 1 and len(urls) == 1:
                notes.append("Muster _pN im Link nicht erkannt - nur das erste Bild wurde eingetragen")
            note = "; ".join(notes) or None
            for u in urls:
                db.insert_image(pid, u)
            db.x("UPDATE pages SET status='fertig', total=?, error=? WHERE id=?", (len(urls), note, pid))
        except Exception as e:                        # noqa: BLE001
            if isinstance(e, LinkNotFound):
                msg = str(e)
            elif isinstance(e, self.PWTimeout):
                msg = "Zeitueberschreitung beim Laden der Seite"
            else:
                msg = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
                self.close_ctx()                      # Kontext evtl. kaputt - beim naechsten Mal neu starten
            retries = row["retries"] + 1
            if retries < 3:
                db.x("UPDATE pages SET status='wartend', retries=?, error=? WHERE id=?", (retries, msg, pid))
                STOP.wait(2.0)
            else:
                db.x("UPDATE pages SET status='fehler', retries=?, error=? WHERE id=?", (retries, msg, pid))
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    # --- Thread-Schleife ---------------------------------------------------
    def run(self):
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright
            self.PWTimeout = PWTimeout
            self.pw = sync_playwright().start()
        except Exception as e:                        # noqa: BLE001
            self.init_error = ("Playwright nicht verfuegbar (pip install playwright; "
                               f"playwright install chromium): {e}")
        while not STOP.is_set():
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd == "login":
                self.do_login()
                continue
            row = db.q("SELECT * FROM pages WHERE status='wartend' ORDER BY id LIMIT 1", one=True)
            if row is None:
                STOP.wait(0.5)
                continue
            self.resolve_page(row)
        self.close_ctx()
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Downloads
# ----------------------------------------------------------------------------
class Aborted(Exception):
    pass


class PermanentError(Exception):
    pass


def fetch(url, part, iid):
    """Laedt url nach 'part' (setzt eine vorhandene .part-Datei per Range fort). Gibt die Bytezahl zurueck."""
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER,
               "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    pos = part.stat().st_size if part.exists() else 0
    if pos:
        headers["Range"] = f"bytes={pos}-"
    try:
        resp = urlopen(Request(url, headers=headers), timeout=30)
    except HTTPError as e:
        if e.code == 416 and pos:                     # .part passt nicht (mehr) -> neu beginnen
            part.unlink(missing_ok=True)
            return fetch(url, part, iid)
        raise
    with resp:
        clen = resp.headers.get("Content-Length")
        clen = int(clen) if clen and clen.isdigit() else None
        if resp.status == 206 and pos:
            mode, done = "ab", pos
            total = pos + clen if clen is not None else None
        else:
            mode, done, total = "wb", 0, clen
        PROGRESS[iid] = (done, total)
        with open(part, mode) as f:
            while True:
                if STOP.is_set():
                    raise Aborted()
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                PROGRESS[iid] = (done, total)
    if total is not None and done != total:
        raise IOError(f"unvollstaendig ({done} von {total} Bytes)")
    return done


def describe(e):
    if isinstance(e, HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, URLError):
        return f"Netzwerk: {e.reason}"
    return f"{type(e).__name__}: {e}"


def schedule_retry(iid, retries, err, min_delay=0):
    max_retries = int(SETTINGS.get("max_retries"))
    if retries >= max_retries:
        db.x("UPDATE images SET status='fehler', error=? WHERE id=?",
             (f"{err} (nach {retries + 1} Versuchen aufgegeben)", iid))
        return
    delay = max(min_delay, BACKOFF[min(retries, len(BACKOFF) - 1)])
    db.x("UPDATE images SET status='wartend', retries=?, not_before=?, error=? WHERE id=?",
         (retries + 1, time.time() + delay, err, iid))


def mark_done(iid, url, name, final, size):
    try:
        db.x("UPDATE images SET status='fertig', url=?, name=?, bytes=?, size=?, path=?, error=NULL WHERE id=?",
             (url, name, size, size, str(final), iid))
    except sqlite3.IntegrityError:                    # URL gibt es schon als eigener Eintrag
        db.x("UPDATE images SET status='fertig', bytes=?, size=?, path=?, error=NULL WHERE id=?",
             (size, size, str(final), iid))


def run_job(row):
    iid, retries = row["id"], row["retries"]
    try:
        dest_dir = Path(SETTINGS.get("download_dir"))
        dest_dir.mkdir(parents=True, exist_ok=True)
        for cand in candidate_urls(row["url"]):
            name = file_name_from_url(cand)
            final = dest_dir / name
            part = dest_dir / (name + ".part")
            if final.exists() and final.stat().st_size > 0:        # schon vorhanden
                mark_done(iid, cand, name, final, final.stat().st_size)
                return
            try:
                size = fetch(cand, part, iid)
            except HTTPError as e:
                if e.code == 404:
                    continue                                       # andere Endung probieren
                if e.code in (401, 403, 410):
                    raise PermanentError(f"HTTP {e.code} (Referer/Anmeldung pruefen)")
                raise
            os.replace(part, final)
            mark_done(iid, cand, name, final, size)
            return
        raise PermanentError("HTTP 404 - Bild nicht gefunden")
    except Aborted:
        db.x("UPDATE images SET status='wartend', not_before=0 WHERE id=?", (iid,))
    except PermanentError as e:
        db.x("UPDATE images SET status='fehler', error=? WHERE id=?", (str(e), iid))
    except Exception as e:                                         # noqa: BLE001
        schedule_retry(iid, retries, describe(e),
                       min_delay=60 if isinstance(e, HTTPError) and e.code == 429 else 0)
    finally:
        PROGRESS.pop(iid, None)


class Downloader(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="downloader")

    def run(self):
        active = []
        while not STOP.is_set():
            active = [t for t in active if t.is_alive()]
            limit = max(1, int(SETTINGS.get("max_parallel")))
            while len(active) < limit and not STOP.is_set():
                row = db.q("SELECT * FROM images WHERE status='wartend' AND not_before<=? ORDER BY id LIMIT 1",
                           (time.time(),), one=True)
                if row is None:
                    break
                db.x("UPDATE images SET status='laedt' WHERE id=?", (row["id"],))
                t = threading.Thread(target=run_job, args=(row,), daemon=True)
                t.start()
                active.append(t)
            STOP.wait(0.3)
        for t in active:
            t.join(timeout=3)


# ----------------------------------------------------------------------------
# API-Funktionen
# ----------------------------------------------------------------------------
def add_urls(text):
    added = 0
    for raw in re.split(r"\s+", (text or "").strip()):
        if not raw.lower().startswith(("http://", "https://")):
            continue
        if is_direct_image(raw):
            added += db.insert_image(None, raw)
        else:
            n = db.x("INSERT OR IGNORE INTO pages(url,status,added) VALUES(?,'wartend',?)", (raw, time.time()))
            if n == 0:
                db.x("UPDATE pages SET status='wartend', retries=0, error=NULL WHERE url=? AND status='fehler'", (raw,))
            added += n
    return added


def apply_settings(data):
    changes = {}
    if "download_dir" in data:
        raw = str(data["download_dir"]).strip().strip('"')
        if not raw:
            raise ValueError("Der Download-Pfad darf nicht leer sein")
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            raise ValueError("Bitte einen absoluten Pfad angeben (z. B. C:\\Users\\Name\\Downloads)")
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise ValueError("In diesen Ordner kann nicht geschrieben werden")
        changes["download_dir"] = str(path)
    if "max_parallel" in data:
        try:
            changes["max_parallel"] = min(10, max(1, int(data["max_parallel"])))
        except (TypeError, ValueError):
            raise ValueError("Parallele Downloads: bitte eine Zahl von 1 bis 10 angeben")
    if changes:
        SETTINGS.update(**changes)
    return {"download_dir": SETTINGS.get("download_dir"), "max_parallel": SETTINGS.get("max_parallel")}


def build_state():
    now = time.time()
    pages = [dict(r) for r in db.q(
        "SELECT p.*, (SELECT COUNT(*) FROM images i WHERE i.page_id=p.id AND i.status='fertig') AS done, "
        "(SELECT COUNT(*) FROM images i WHERE i.page_id=p.id) AS n FROM pages p ORDER BY p.id")]
    rows = db.q("SELECT * FROM images ORDER BY id DESC LIMIT 2000")[::-1]
    images = []
    for r in rows:
        d = dict(r)
        prog = PROGRESS.get(r["id"])
        if r["status"] == "laedt" and prog:
            d["bytes"], d["size"] = prog[0], prog[1] or 0
        d["retry_in"] = max(0, round(r["not_before"] - now)) if r["status"] == "wartend" and r["retries"] else 0
        images.append(d)
    counts = {r["status"]: r["c"] for r in db.q("SELECT status, COUNT(*) AS c FROM images GROUP BY status")}
    return {
        "settings": {"download_dir": SETTINGS.get("download_dir"), "max_parallel": SETTINGS.get("max_parallel"),
                     "max_retries": SETTINGS.get("max_retries")},
        "pages": pages, "images": images, "counts": counts,
        "message": UI["message"] or (RESOLVER.init_error if RESOLVER and pages and RESOLVER.init_error else ""),
        "login_open": UI["login_open"],
    }


def retry_failed():
    a = db.x("UPDATE images SET status='wartend', retries=0, not_before=0, error=NULL WHERE status='fehler'")
    b = db.x("UPDATE pages SET status='wartend', retries=0, error=NULL WHERE status='fehler'")
    return a + b


def clear_done():
    db.x("DELETE FROM images WHERE status='fertig'")
    db.x("DELETE FROM pages WHERE status='fertig' AND id NOT IN (SELECT DISTINCT page_id FROM images WHERE page_id IS NOT NULL)")


def remove_item(data):
    if data.get("page_id") is not None:
        pid = int(data["page_id"])
        db.x("DELETE FROM images WHERE page_id=?", (pid,))
        db.x("DELETE FROM pages WHERE id=?", (pid,))
    elif data.get("image_id") is not None:
        db.x("DELETE FROM images WHERE id=?", (int(data["image_id"]),))


# ----------------------------------------------------------------------------
# HTTP-Server
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BilderDownloader/1.0"

    def log_message(self, *args):
        pass

    def _allowed(self):
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            if (o.hostname or "") not in ("127.0.0.1", "localhost"):
                self.send_error(403)
                return False
        return True

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._allowed():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(build_state())
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._allowed():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "Ungueltige Anfrage"}, 400)
        path = urlparse(self.path).path
        try:
            if path == "/api/add":
                self._json({"added": add_urls(data.get("text", ""))})
            elif path == "/api/settings":
                self._json(apply_settings(data))
            elif path == "/api/retry_failed":
                self._json({"changed": retry_failed()})
            elif path == "/api/clear_done":
                clear_done()
                self._json({"ok": True})
            elif path == "/api/remove":
                remove_item(data)
                self._json({"ok": True})
            elif path == "/api/login":
                if RESOLVER.init_error:
                    return self._json({"error": RESOLVER.init_error}, 400)
                if UI["login_open"]:
                    return self._json({"error": "Das Anmelde-Fenster ist schon offen"}, 400)
                RESOLVER.cmds.put("login")
                self._json({"ok": True})
            elif path == "/api/quit":
                self._json({"ok": True})
                SETTINGS.save()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:                        # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# ----------------------------------------------------------------------------
# Oberflaeche (Vanilla-JS, keine externen Ressourcen)
# ----------------------------------------------------------------------------
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bild-Downloader</title>
<style>
:root{--bg:#0f1218;--panel:#171c26;--line:#262d3b;--text:#e6e9ef;--mute:#8b95a8;--acc:#5b9dff;--ok:#43c27a;--err:#ef5b5b;--warn:#e8b04a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mute);margin-bottom:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 10px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.grow{flex:1 1 320px}
label{color:var(--mute);font-size:12px;display:block;margin-bottom:4px}
input[type=text],input[type=number],textarea{width:100%;background:#0d1117;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px 10px;font:inherit}
input[type=text]{font-family:ui-monospace,Consolas,Menlo,monospace;font-size:13px}
input[type=number]{width:80px}
textarea{min-height:84px;resize:vertical;font-family:ui-monospace,Consolas,Menlo,monospace;font-size:12px}
input:focus,textarea:focus{outline:1px solid var(--acc)}
button{background:#222a3a;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px 14px;font:inherit;cursor:pointer}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#07101f;font-weight:600}
button.x{padding:2px 8px;color:var(--mute)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 0}
.chip{background:#0d1117;border:1px solid var(--line);border-radius:99px;padding:2px 10px;font-size:12px;color:var(--mute)}
.chip b{color:var(--text)}
#msg{display:none;margin-bottom:14px;padding:10px 14px;border-radius:8px;background:#2a2112;border:1px solid #5a4417;color:var(--warn)}
#dirInfo{font-size:12px;margin-top:4px;min-height:16px}
.ok{color:var(--ok)}.bad{color:var(--err)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--mute);font-weight:500;font-size:12px}
td.url{max-width:480px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,Consolas,Menlo,monospace;font-size:12px}
button.copy{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;background:none;border:1px solid transparent;border-radius:5px;padding:2px 6px;color:var(--text);font-family:ui-monospace,Consolas,Menlo,monospace;font-size:12px}
button.copy:hover{border-color:var(--acc);background:#0d1117}
button.copy.copied{color:var(--ok);border-color:var(--ok)}
.tbl{max-height:380px;overflow:auto}
.st{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line)}
.st-fertig{color:var(--ok);border-color:#1f5a3a}.st-fehler{color:var(--err);border-color:#6a2a2a}
.st-laedt,.st-aufloesen{color:var(--acc);border-color:#264a7a}.st-wartend{color:var(--mute)}
.small{font-size:12px;color:var(--mute)}
.bar{height:6px;background:#0d1117;border-radius:4px;min-width:120px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc)}
.empty{color:var(--mute);padding:10px 8px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Bild-Downloader</h1>
  <div class="sub">Seiten-Links einfügen, die Bilder werden automatisch ermittelt und heruntergeladen.</div>
  <div id="msg"></div>

  <div class="panel">
    <h2>Einstellungen</h2>
    <div class="row">
      <div class="grow">
        <label for="dir">Download-Ordner</label>
        <input type="text" id="dir" spellcheck="false" autocomplete="off">
        <div id="dirInfo"></div>
      </div>
      <div>
        <label for="par">Gleichzeitig (1–10)</label>
        <input type="number" id="par" min="1" max="10" value="3">
      </div>
      <div>
        <label>&nbsp;</label>
        <button id="login">Bei pixiv anmelden …</button>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>Links hinzufügen</h2>
    <textarea id="links" placeholder="Ein Link pro Zeile, z. B. https://www.pixiv.net/artworks/123456789"></textarea>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="add">Hinzufügen</button>
      <button id="retry">Fehlgeschlagene erneut</button>
      <button id="clear">Fertige entfernen</button>
      <span style="flex:1"></span>
      <button id="quit">Beenden</button>
    </div>
    <div class="chips" id="chips"></div>
  </div>

  <div class="panel">
    <h2>Seiten</h2>
    <div class="tbl" id="pages"></div>
  </div>

  <div class="panel">
    <h2>Downloads</h2>
    <div class="tbl" id="images"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = b => { if (b == null) return "–"; const u = ["B","KB","MB","GB"]; let i = 0, v = b; while (v >= 1024 && i < 3) { v /= 1024; i++; } return v.toFixed(i ? 1 : 0) + " " + u[i]; };
const LABEL = {wartend:"wartet", aufloesen:"löst auf …", laedt:"lädt", fertig:"fertig", fehler:"Fehler"};
let inited = false, lastKey = "";

async function api(path, body) {
  const opt = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  let d = {}; try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

// Nur neu zeichnen, wenn sich der Inhalt geaendert hat - so bleibt markierter Text stehen.
const lastHtml = {};
function setHtml(sel, html) {
  if (lastHtml[sel] === html) return;
  lastHtml[sel] = html;
  $(sel).innerHTML = html;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
  ta.remove(); return ok;
}

function stBadge(st) { return '<span class="st st-' + esc(st) + '">' + esc(LABEL[st] || st) + '</span>'; }

function render(s) {
  if (!inited) {
    $("#dir").value = s.settings.download_dir;
    $("#par").value = s.settings.max_parallel;
    inited = true;
  }
  const m = $("#msg");
  m.style.display = s.message ? "block" : "none";
  m.textContent = s.message || "";
  $("#login").disabled = !!s.login_open;

  const c = s.counts, n = k => c[k] || 0;
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  setHtml("#chips",
    '<span class="chip">Bilder gesamt <b>' + total + '</b></span>' +
    '<span class="chip">lädt <b>' + n("laedt") + '</b></span>' +
    '<span class="chip">wartet <b>' + n("wartend") + '</b></span>' +
    '<span class="chip">fertig <b>' + n("fertig") + '</b></span>' +
    '<span class="chip">Fehler <b>' + n("fehler") + '</b></span>');

  setHtml("#pages", s.pages.length ? '<table><tr><th>Seite</th><th>Bilder</th><th>Status</th><th></th></tr>' +
    s.pages.map(p =>
      '<tr><td class="url"><button class="copy" title="In die Zwischenablage kopieren" data-copy="' + esc(p.url) + '">' + esc(p.url) + '</button></td>' +
      '<td>' + (p.total ? p.done + " / " + p.total : "–") + '</td>' +
      '<td>' + stBadge(p.status) + (p.error ? ' <span class="small">' + esc(p.error) + '</span>' : '') + '</td>' +
      '<td><button class="x" title="Entfernen" data-rm-page="' + p.id + '">✕</button></td></tr>').join("") + '</table>'
    : '<div class="empty">Noch keine Seiten.</div>');

  setHtml("#images", s.images.length ? '<table><tr><th>Datei</th><th>Status</th><th>Fortschritt</th><th>Größe</th><th>Hinweis</th><th></th></tr>' +
    s.images.map(i => {
      const pct = i.size ? Math.min(100, Math.round(i.bytes / i.size * 100)) : (i.status === "fertig" ? 100 : 0);
      let note = i.error || "";
      if (i.status === "wartend" && i.retries) note = "Versuch " + (i.retries + 1) + " in " + i.retry_in + " s – " + note;
      return '<tr><td title="' + esc(i.url) + '">' + esc(i.name) + '</td>' +
        '<td>' + stBadge(i.status) + '</td>' +
        '<td><div class="bar"><i style="width:' + pct + '%"></i></div></td>' +
        '<td class="small">' + (i.status === "laedt" ? fmt(i.bytes) + " / " + fmt(i.size || null) : fmt(i.size || null)) + '</td>' +
        '<td class="small">' + esc(note) + '</td>' +
        '<td><button class="x" title="Entfernen" data-rm-img="' + i.id + '">✕</button></td></tr>';
    }).join("") + '</table>'
    : '<div class="empty">Noch keine Downloads.</div>');
}

async function poll() {
  try { render(await api("/api/state")); } catch (e) { /* Server evtl. beendet */ }
  setTimeout(poll, 1000);
}

async function saveSettings() {
  const info = $("#dirInfo");
  try {
    const r = await api("/api/settings", {download_dir: $("#dir").value, max_parallel: $("#par").value});
    $("#dir").value = r.download_dir; $("#par").value = r.max_parallel;
    info.className = "ok"; info.textContent = "Gespeichert.";
    setTimeout(() => { info.textContent = ""; }, 2500);
  } catch (e) {
    info.className = "bad"; info.textContent = e.message;
  }
}

$("#dir").addEventListener("change", saveSettings);
$("#dir").addEventListener("keydown", e => { if (e.key === "Enter") e.target.blur(); });
$("#par").addEventListener("change", saveSettings);

$("#add").addEventListener("click", async () => {
  const t = $("#links").value;
  if (!t.trim()) return;
  try { const r = await api("/api/add", {text:t}); $("#links").value = ""; }
  catch (e) { alert(e.message); }
});
$("#links").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#add").click(); });
$("#retry").addEventListener("click", () => api("/api/retry_failed", {}));
$("#clear").addEventListener("click", () => api("/api/clear_done", {}));
$("#login").addEventListener("click", async () => { try { await api("/api/login", {}); } catch (e) { alert(e.message); } });
$("#quit").addEventListener("click", async () => {
  if (!confirm("Bild-Downloader beenden? Offene Downloads werden beim nächsten Start fortgesetzt.")) return;
  try { await api("/api/quit", {}); } catch (e) {}
  document.body.innerHTML = '<div class="wrap"><h1>Beendet</h1><div class="sub">Du kannst diesen Tab schließen.</div></div>';
});
document.addEventListener("click", async e => {
  const c = e.target.closest("[data-copy]");
  if (c) {
    const old = c.textContent, ok = await copyText(c.dataset.copy);
    c.textContent = ok ? "✓ kopiert" : "Kopieren nicht möglich";
    c.classList.toggle("copied", ok);
    setTimeout(() => { c.textContent = old; c.classList.remove("copied"); }, 1200);
    return;
  }
  const p = e.target.closest("[data-rm-page]"), i = e.target.closest("[data-rm-img]");
  if (p) api("/api/remove", {page_id: +p.dataset.rmPage});
  if (i) api("/api/remove", {image_id: +i.dataset.rmImg});
});

// Beim Schliessen des Tabs Einstellungen nochmals sichern (zusaetzlich zum Speichern bei jeder Aenderung)
window.addEventListener("beforeunload", () => {
  if (!inited) return;
  try { navigator.sendBeacon("/api/settings", new Blob([JSON.stringify({download_dir: $("#dir").value, max_parallel: $("#par").value})], {type:"application/json"})); } catch (e) {}
});

poll();
</script>
</body>
</html>
'''


# ----------------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------------
def init(base_dir=None):
    global DATA_DIR, DB_PATH, SETTINGS_PATH, PROFILE_DIR, SETTINGS, db
    DATA_DIR = Path(base_dir) if base_dir else APP_DIR / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / "links.db"
    SETTINGS_PATH = DATA_DIR / "settings.json"
    PROFILE_DIR = DATA_DIR / "browser_profile"
    SETTINGS = Settings()
    SETTINGS.save()
    db = DB(DB_PATH)


def main():
    global RESOLVER
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        url = f"http://{HOST}:{PORT}/"
        print(f"Port {PORT} ist belegt - vermutlich laeuft der Bild-Downloader schon: {url}")
        webbrowser.open(url)
        return 1

    init()
    atexit.register(lambda: SETTINGS and SETTINGS.save())
    RESOLVER = Resolver()
    downloader = Downloader()
    RESOLVER.start()
    downloader.start()

    url = f"http://{HOST}:{PORT}/"
    print(f"Bild-Downloader laeuft: {url}   (Beenden: Button in der Oberflaeche oder Strg+C)")
    print(f"Daten: {DATA_DIR}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        SETTINGS.save()
        downloader.join(timeout=5)
        RESOLVER.join(timeout=8)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
