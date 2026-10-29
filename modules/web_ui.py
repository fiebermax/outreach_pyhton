"""Kleine lokale Oberfläche: Website eingeben → Screenshots + Design-Urteil; Firmenliste; Excel-Download.

Start: `python main.py ui` → http://127.0.0.1:8765 (nur auf diesem Rechner erreichbar).
Bewusst ohne Web-Framework: Standardbibliothek, eine Seite pro Ansicht.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from config import Settings
from modules.database import Database
from modules.design_review import DesignReview, domain_for, load_review, review_dir, review_site
from modules.excel_export import COLUMNS, collect_rows, export_excel
from modules.llm import LLMClient

log = logging.getLogger(__name__)
SCREENSHOTS = {"desktop.png", "mobile.png"}
COL = {name: i for i, (name, _) in enumerate(COLUMNS)}

CSS = """
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --line:#e3e3e0; --accent:#2f5d50;
        --bad:#b3261e; --badbg:#fdecea; --ok:#1e6b3a; --okbg:#e7f4ec; --warnbg:#fff4d6; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
header { background:var(--card); border-bottom:1px solid var(--line); padding:14px 24px; display:flex; gap:8px 24px;
         align-items:center; flex-wrap:wrap; }
header a { color:var(--text); text-decoration:none; font-weight:600; }
header nav a { font-weight:400; color:var(--muted); margin-right:16px; }
main { max-width:1200px; margin:0 auto; padding:24px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:20px; margin-bottom:20px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:0 0 12px; }
.muted { color:var(--muted); font-size:13px; }
form.check { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
input[type=text] { flex:1 1 260px; min-width:0; padding:10px 12px; border:1px solid var(--line); border-radius:8px; font-size:15px; }
button, .btn { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:9px 14px; font-size:14px;
               cursor:pointer; text-decoration:none; display:inline-block; white-space:nowrap; }
.btn.light { background:transparent; color:var(--accent); border:1px solid var(--accent); }
table { width:100%; border-collapse:collapse; font-size:14px; }
td a { word-break:break-all; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
tr.manual td:first-child { box-shadow: inset 3px 0 0 #e0a800; }
.pill { display:inline-block; padding:2px 8px; border-radius:99px; font-size:12px; font-weight:600; }
.pill.yes { background:var(--warnbg); color:#7a5b00; } .pill.no { background:var(--okbg); color:var(--ok); }
.verdict { font-size:28px; font-weight:700; }
.verdict.bad { color:var(--bad); } .verdict.ok { color:var(--ok); }
.shots { display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap; }
.shot { border:1px solid var(--line); border-radius:8px; overflow:auto; max-height:640px; background:#fff; }
.shot img { display:block; }
.shot.desktop img { width:760px; max-width:100%; } .shot.mobile img { width:260px; }
ul.bad li::marker { color:var(--bad); } ul.good li::marker { color:var(--ok); }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.hook { background:var(--okbg); border-radius:8px; padding:12px 14px; }
.error { background:var(--badbg); color:var(--bad); border-radius:8px; padding:10px 14px; }
details pre { font-size:12px; white-space:pre-wrap; background:var(--bg); padding:10px; border-radius:6px; }
@media (max-width: 800px) { .cols { grid-template-columns:1fr; } main { padding:16px; } .card { padding:16px; }
                             .hide-sm { display:none; } th, td { padding:8px 4px; } }
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><title>{_e(title)}</title><style>{CSS}</style></head>"
            f"<body><header><a href='/'>Website-Check</a><nav><a href='/'>Übersicht</a>"
            f"<a href='/excel'>Excel herunterladen</a></nav></header><main>{body}</main></body></html>").encode()


def _check_form(url: str = "", label: str = "Screenshot & Design prüfen") -> str:
    return (f"<form class='check' method='post' action='/pruefen'><input type='text' name='url' required "
            f"placeholder='https://www.beispiel-schreinerei.de' value='{_e(url)}'><button>{_e(label)}</button></form>")


def render_index(db: Database, settings: Settings, ai_available: bool) -> bytes:
    firms, _ = collect_rows(db, settings.data_dir)
    manual = sum(1 for r in firms if r[COL["Manuell prüfen"]] == "Ja")
    ai_note = ("Das Urteil schreibt DeepSeek anhand gemessener Design-Signale (die API kann keine Bilder ansehen)."
               if ai_available else "DEEPSEEK_API_KEY fehlt – das Urteil kommt nur aus den festen Regeln.")
    rows = []
    for r in firms:
        website = r[COL["Website"]] or ""
        is_manual = r[COL["Manuell prüfen"]] == "Ja"
        actions = ""
        if website.startswith("http"):
            domain = domain_for(website)
            actions = (f"<form method='post' action='/pruefen' style='display:inline'><input type='hidden' name='url' "
                       f"value='{_e(website)}'><button>Prüfen</button></form>")
            if load_review(settings.data_dir, domain):
                actions += f" <a class='btn light' href='/bericht?d={quote(domain)}'>Bericht</a>"
        link = f"<a href='{_e(website)}' target=_blank rel=noopener>{_e(website)}</a>" if website.startswith("http") else "–"
        rows.append(
            f"<tr class='{'manual' if is_manual else ''}'><td>{_e(r[COL['Firma']])}<div class='muted'>"
            f"{_e(r[COL['Branche']] or '')} · {_e(r[COL['Ort']] or '')}</div></td><td>{link}</td>"
            f"<td><span class='pill {'yes' if is_manual else 'no'}'>{'manuell prüfen' if is_manual else 'Entwurf fertig'}"
            f"</span><div class='muted'>{_e(r[COL['Grund']])}</div></td>"
            f"<td class='hide-sm'>{_e(r[COL['Design-Urteil']]) or '–'}</td><td>{actions}</td></tr>")
    table = ("<table><tr><th>Firma</th><th>Website</th><th>Status</th><th class='hide-sm'>Design</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class='muted'>Noch keine Firmen in der Datenbank.</p>"
    body = (f"<div class='card'><h1>Website prüfen</h1><div class='muted'>Macht Screenshots (Computer + Handy) und "
            f"beurteilt das Design: veraltet, unattraktiv, nicht fürs Handy gemacht? {_e(ai_note)}</div>"
            f"{_check_form()}</div>"
            f"<div class='card'><h2>Firmen ({len(firms)}, davon {manual} manuell prüfen)</h2>{table}</div>")
    return _page("Website-Check", body)


def render_report(review: DesignReview) -> bytes:
    d = quote(review.domain)
    tone = "bad" if review.verdict in ("veraltet", "sehr veraltet") else "ok"
    shots = ""
    for name, label, cls in ((review.desktop_png, "Computer", "desktop"), (review.mobile_png, "Handy", "mobile")):
        if name:
            shots += (f"<div><div class='muted'>{label} · <a href='/datei?d={d}&f={name}&download=1'>herunterladen</a>"
                      f"</div><div class='shot {cls}'><img src='/datei?d={d}&f={name}' alt='Screenshot {label}'>"
                      f"</div></div>")
    bad = "".join(f"<li>{_e(x)}</li>" for x in review.bad) or "<li>nichts Auffälliges</li>"
    good = "".join(f"<li>{_e(x)}</li>" for x in review.good) or "<li>–</li>"
    source = (f"Urteil: DeepSeek ({_e(review.ai_model)}), auf Basis gemessener Signale – den Screenshot "
              "selbst kann die KI nicht sehen." if review.ai_model else "Urteil aus festen Regeln (ohne KI).")
    body = (
        f"<div class='card'><div class='muted'><a href='/'>← Übersicht</a></div>"
        f"<h1>{_e(review.domain)}</h1><div class='muted'><a href='{_e(review.final_url or review.url)}' target=_blank "
        f"rel=noopener>{_e(review.final_url or review.url)}</a> · geprüft {_e(review.created_at[:16].replace('T', ' '))} UTC"
        f"</div>{_check_form(review.url, 'Neu prüfen')}</div>"
        + (f"<div class='error'>{_e(review.error)}</div><br>" if review.error else "")
        + f"<div class='card'><div class='verdict {tone}'>{_e(review.summary)}</div>"
        + (f"<div class='muted'>{_e(review.estimated_era)}</div>" if review.estimated_era else "")
        + f"<div class='muted'>{source}</div><br><div class='cols'><div><h2>Schlecht</h2><ul class='bad'>{bad}</ul></div>"
        f"<div><h2>Gut</h2><ul class='good'>{good}</ul></div></div>"
        + (f"<br><div class='hook'><strong>Aufhänger fürs Gespräch:</strong> {_e(review.hook)}</div>" if review.hook else "")
        + f"</div><div class='card'><h2>Screenshots</h2><div class='shots'>{shots or '–'}</div></div>"
        f"<div class='card'><details><summary>Messwerte</summary><pre>"
        f"{_e(json.dumps(review.signals, ensure_ascii=False, indent=2))}</pre></details></div>")
    return _page(f"Design: {review.domain}", body)


def make_handler(settings: Settings, llm_factory: Callable[[], LLMClient | None]):
    ai_available = settings.deepseek_api_key is not None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("ui: " + fmt, *args)

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/html; charset=utf-8",
                  headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/":
                db = Database(settings.database_path)
                try:
                    return self._send(200, render_index(db, settings, ai_available))
                finally:
                    db.close()
            if parts.path == "/bericht":
                review = load_review(settings.data_dir, q.get("d"))
                if review is None:
                    return self._send(404, _page("Nicht gefunden", "<div class='card'>Kein Bericht. "
                                                                   "<a href='/'>Zur Übersicht</a></div>"))
                return self._send(200, render_report(review))
            if parts.path == "/datei":
                name = q.get("f", "")
                if name not in SCREENSHOTS or not q.get("d"):
                    return self._send(404)
                path = review_dir(settings.data_dir, q["d"]) / name
                if not path.is_file():
                    return self._send(404)
                headers = {"Content-Disposition": f'attachment; filename="{q["d"]}-{name}"'} if q.get("download") else {}
                return self._send(200, path.read_bytes(), "image/png", headers)
            if parts.path == "/excel":
                db = Database(settings.database_path)
                try:
                    path, _, _ = export_excel(db, settings.data_dir / "firmen.xlsx", settings.data_dir)
                finally:
                    db.close()
                return self._send(200, path.read_bytes(),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  {"Content-Disposition": 'attachment; filename="firmen.xlsx"'})
            return self._send(404, _page("Nicht gefunden", "<div class='card'>Seite nicht gefunden.</div>"))

        def do_POST(self):
            if urlsplit(self.path).path != "/pruefen":
                return self._send(404)
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            url = (parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("url") or [""])[0].strip()
            if not url:
                return self._send(303, headers={"Location": "/"})
            if "://" not in url:
                url = "https://" + url
            review = asyncio.run(review_site(url, settings.data_dir, llm_factory()))
            return self._send(303, headers={"Location": f"/bericht?d={quote(review.domain)}"})

    return Handler


def serve(settings: Settings, llm_factory: Callable[[], LLMClient | None], port: int = 8765,
          open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(settings, llm_factory))
    url = f"http://127.0.0.1:{port}"
    print(f"Oberfläche läuft auf {url} – beenden mit Strg+C")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
