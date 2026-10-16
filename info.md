
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
  5. Animierte Werke (Ugoira, im Browser ein <canvas>): Der Netzwerkverkehr der Seite wird
     mitgelesen (ugoira_meta + ZIP mit Einzelbildern). Die ZIP wird geladen und mit ffmpeg
     (muss im PATH liegen oder in settings.json unter "ffmpeg_path" stehen) zu MP4/WebM mit
     den Original-Bildzeiten zusammengesetzt.

