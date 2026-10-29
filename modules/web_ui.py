"""Kleine lokale Oberfläche: Website eingeben → Screenshots + Design-Urteil; Firmenliste; Excel-Download.

Start: `python main.py ui` → http://127.0.0.1:8765 (nur auf diesem Rechner erreichbar).
Bewusst ohne Web-Framework: Standardbibliothek, eine Seite pro Ansicht.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import threading
from collections.abc import Callable
from contextlib import redirect_stdout
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from config import Settings
from core.niche import load_niche
from modules.database import Channel, Database, Outcome
from modules.design_review import DesignReview, domain_for, load_review, review_dir, review_site
from modules.excel_export import COLUMNS, collect_rows, export_excel
from modules.llm import LLMClient

log = logging.getLogger(__name__)
SCREENSHOTS = {"desktop.png", "mobile.png"}
JOB = {"running": False, "lines": []}                    # ein Suchlauf zur Zeit
NOTICE = {"text": ""}                                     # letzte Meldung (z. B. Postfach-Abgleich)
MAIL_STATE = {"sent": "gesendet", "replied": "Antwort erhalten", "interested": "interessiert",
              "not_interested": "kein Interesse", "meeting": "Termin", "customer": "Kunde"}
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
textarea, select, input[type=number] { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:inherit; }
.grid4 { display:grid; grid-template-columns:2fr 2fr 1fr 1fr auto; gap:8px; align-items:end; }
@media (max-width:800px) { .grid4 { grid-template-columns:1fr; } }
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
    ai_note = ("Das Urteil schreibt DeepSeek nach Ansicht der Screenshots."
               if ai_available else "DEEPSEEK_API_KEY fehlt – das Urteil kommt nur aus den festen Regeln.")
    mail_state = {row["company_id"]: row["outcome"] for row in db.conn.execute(
        "SELECT company_id, outcome FROM contact_events ORDER BY occurred_at")}
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
            f"<tr class='{'manual' if is_manual else ''}'><td><a href='/firma?id={r[COL['ID']]}'>"
            f"{_e(r[COL['Firma']])}</a><div class='muted'>"
            f"{_e(r[COL['Branche']] or '')} · {_e(r[COL['Ort']] or '')}</div></td><td>{link}</td>"
            f"<td><span class='pill {'yes' if is_manual else 'no'}'>"
            f"{_e(MAIL_STATE.get(mail_state.get(r[COL['ID']]), 'manuell prüfen' if is_manual else 'Entwurf fertig'))}"
            f"</span><div class='muted'>{_e(r[COL['Grund']])}</div></td>"
            f"<td class='hide-sm'>{_e(r[COL['Design-Urteil']]) or '–'}</td><td>{actions}</td></tr>")
    table = ("<table><tr><th>Firma</th><th>Website</th><th>Status</th><th class='hide-sm'>Design</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else "<p class='muted'>Noch keine Firmen in der Datenbank.</p>"
    branches = "".join(f"<option>{_e(b.name)}</option>" for b in load_niche().branche)
    run_card = (f"<div class='card'><h1>Neue Suche</h1><div class='muted'>Sucht Firmen, crawlt die Websites, macht "
                f"Screenshots + Design-Urteil und schreibt E-Mail-Entwürfe. Es wird nichts versendet.</div><br>"
                f"<form method='post' action='/lauf' class='grid4'><label>Ort<input type='text' name='ort' "
                f"placeholder='{_e(load_niche().region.ort)}'></label><label>Branche<select name='branche'>"
                f"<option value=''>alle</option>{branches}</select></label><label>Suchanfragen<input type='number' "
                f"name='max' value='4' min='1' max='30'></label><label><input type='checkbox' name='design' checked> "
                f"Design prüfen</label><button>Suche starten</button></form>"
                + (f"<p><a href='/lauf'>Läuft gerade … Fortschritt ansehen</a></p>" if JOB["running"] else "")
                + f"<br><form method='post' action='/scan' style='display:inline'><button class='btn light'>"
                f"Alle „manuell prüfen“ scannen</button></form> "
                + f"<form method='post' action='/postfach' style='display:inline'><button class='btn light'>"
                f"Postfach abgleichen (gesendet / Antworten)</button></form> <span class='muted'>{_e(NOTICE['text'])}"
                f"</span></div>")
    contacts_card = ("<div class='card'><h2>Bisherige Kontakte abgleichen</h2><div class='muted'>CSV mit Spalten "
                     "firma;website;email;datum;kanal;ergebnis;notiz (Vorlage: docs/kontakte_vorlage.csv). Danach "
                     "zeigt jede Firma, ob du sie schon angeschrieben hast, und es entsteht kein doppelter Entwurf."
                     "</div><form method='post' action='/kontakte'><input type='file' accept='.csv,.txt' onchange=\""
                     "const f=this.files[0];if(f){const r=new FileReader();r.onload=e=>{this.form.csv.value="
                     "e.target.result};r.readAsText(f)}\"><br><br><textarea name='csv' rows='4' placeholder='oder hier "
                     "einfügen'></textarea><br><br><button>Abgleichen</button></form></div>")
    body = (run_card + contacts_card + f"<div class='card'><h1>Website prüfen</h1><div class='muted'>Macht Screenshots (Computer + Handy) und "
            f"beurteilt das Design: veraltet, unattraktiv, nicht fürs Handy gemacht? {_e(ai_note)}</div>"
            f"{_check_form()}</div>"
            f"<div class='card'><h2>Firmen ({len(firms)}, davon {manual} manuell prüfen)</h2>{table}</div>")
    return _page("Website-Check", body)


def _sender(settings: Settings):
    from modules.pitch import Sender
    return Sender(name=settings.sender_name, region=load_niche().region.ort, email=settings.sender_address,
                  website=settings.sender_website)


def _contact_status(db: Database, cid: int | None) -> str:
    """Abgleich mit der eigenen Kontakthistorie: schon angeschrieben?"""
    if cid is None:
        return "<p class='pill no'>Noch nie kontaktiert (nicht in der Datenbank)</p>"
    history = db.contact_history(cid)
    if not history:
        return "<p class='pill no'>Noch nie kontaktiert</p>"
    last = history[-1]
    return (f"<p class='pill yes'>Schon kontaktiert: {_e(last['occurred_at'][:10])} per {_e(last['channel'])} → "
            f"{_e(MAIL_STATE.get(last['outcome'], last['outcome']))} ({len(history)}×)</p>")


def render_mail_card(db: Database, settings: Settings, review: DesignReview) -> str:
    from modules.pitch import build_design_draft
    from services.outreach_service import choose_recipient
    row = db.conn.execute("SELECT id FROM companies WHERE domain = ?", (review.domain,)).fetchone()
    cid = row["id"] if row else None
    recipient = (choose_recipient(db, cid) if cid else None) or ""
    observation = review.hook or (review.bad[0] if review.bad else "")
    if not observation:
        return ""
    try:
        draft = build_design_draft(review.domain, observation, recipient or f"info@{review.domain}", _sender(settings))
    except ValueError:                     # Domain ohne gültige Adresse (z. B. IP)
        return ""
    return (f"<div class='card'><h2>E-Mail</h2>{_contact_status(db, cid)}"
            f"<form method='post' action='/bericht_entwurf'><input type='hidden' name='domain' value='{_e(review.domain)}'>"
            f"<input type='hidden' name='url' value='{_e(review.final_url or review.url)}'>"
            f"<label>An<input type='text' name='empfaenger' value='{_e(recipient)}' placeholder='info@{_e(review.domain)}'>"
            f"</label><br><br><label>Betreff<input type='text' name='betreff' value='{_e(draft.subject)}'></label><br><br>"
            f"<textarea name='text' rows='16'>{_e(draft.body)}</textarea><br><br>"
            f"<button>Als Entwurf speichern</button></form></div>")


def render_report(review: DesignReview, mail_card: str = "") -> bytes:
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
    source = (f"Urteil: DeepSeek ({_e(review.ai_model)}) nach Ansicht der Screenshots."
              if review.ai_model else "Urteil aus festen Regeln (ohne KI).")
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
        + f"</div>{mail_card}<div class='card'><h2>Screenshots</h2><div class='shots'>{shots or '–'}</div></div>"
        f"<div class='card'><details><summary>Messwerte</summary><pre>"
        f"{_e(json.dumps(review.signals, ensure_ascii=False, indent=2))}</pre></details></div>")
    return _page(f"Design: {review.domain}", body)


def render_company(db: Database, settings: Settings, cid: int) -> bytes:
    c = db.get_company(cid)
    if c is None:
        return _page("Nicht gefunden", "<div class='card'>Firma nicht gefunden.</div>")
    row = next((r for r in collect_rows(db, settings.data_dir)[0] if r[COL["ID"]] == cid), None)
    draft = db.conn.execute("SELECT * FROM email_drafts WHERE company_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    review = load_review(settings.data_dir, domain_for(c["website"])) if (c["website"] or "").startswith("http") else None
    report_id = db.latest_report_id(cid)
    findings = db.get_report(report_id).findings if report_id else []
    history = "".join(f"<li>{_e(h['occurred_at'][:10])} · {_e(h['channel'])} → {_e(MAIL_STATE.get(h['outcome'], h['outcome']))}"
                      f"{' · ' + _e(h['note']) if h['note'] else ''}</li>" for h in db.contact_history(cid))
    info = (f"<div class='card'><div class='muted'><a href='/'>← Übersicht</a></div><h1>{_e(c['name'])}</h1>"
            f"<div class='muted'>{_e(c['address'] or '')} · {_e(c['phone'] or '')} · {_e(row[COL['E-Mail']] if row else '')}"
            f"</div>" + (f"<p><a href='{_e(c['website'])}' target=_blank rel=noopener>{_e(c['website'])}</a></p>"
                         if (c["website"] or "").startswith("http") else "<p>keine Website</p>")
            + (f"<p><strong>Manuell prüfen:</strong> {_e(row[COL['Grund']])}</p>" if row and row[COL['Grund']] else "")
            + "</div>")
    design = ""
    if review:
        design = (f"<div class='card'><h2>Design: {_e(review.summary)}</h2>"
                  f"<ul class='bad'>{''.join(f'<li>{_e(x)}</li>' for x in review.bad[:5])}</ul>"
                  + (f"<div class='hook'>{_e(review.hook)}</div>" if review.hook else "")
                  + f"<p><a href='/bericht?d={quote(review.domain)}'>Screenshots & ganzer Bericht</a></p>")
        if review.hook and draft is None:
            design += ("<form method='post' action='/firma'><input type='hidden' name='id' value='" + str(cid) + "'>"
                       "<input type='hidden' name='aktion' value='design_entwurf'><button>E-Mail-Entwurf aus dem "
                       "Design-Urteil erstellen</button></form>")
        design += "</div>"
    elif (c["website"] or "").startswith("http"):
        design = f"<div class='card'>Noch kein Design-Urteil. {_check_form(c['website'])}</div>"
    found = "".join(f"<li><strong>{_e(f.category.value)}</strong>: {_e(f.description)} <span class='muted'>{_e(f.evidence)}"
                    f"</span></li>" for f in findings) or "<li>keine</li>"
    mail = "<div class='card'><h2>E-Mail</h2><p class='muted'>Noch kein Entwurf.</p></div>"
    if draft:
        hidden = f"<input type='hidden' name='id' value='{cid}'><input type='hidden' name='draft' value='{draft['id']}'>"
        buttons = "".join(f"<button name='aktion' value='{k}' class='btn light'>{_e(v)}</button> "
                          for k, v in (("sent", "gesendet"), ("replied", "Antwort erhalten"),
                                       ("interested", "interessiert"), ("not_interested", "kein Interesse")))
        mail = (f"<div class='card'><h2>E-Mail an {_e(draft['recipient'])} ({_e(draft['status'])})</h2>"
                f"<form method='post' action='/firma'>{hidden}<label>Betreff<input type='text' name='betreff' "
                f"value='{_e(draft['subject'])}'></label><br><br><textarea name='text' rows='18'>{_e(draft['body'])}"
                f"</textarea><br><br><button name='aktion' value='speichern'>Speichern</button> "
                f"<button name='aktion' value='postfach' class='btn light'>Als Entwurf ins Postfach legen</button>"
                f"<p class='muted'>Status von Hand setzen (oder oben „Postfach abgleichen“):</p>{buttons}</form>"
                f"{'<ul>' + history + '</ul>' if history else ''}</div>")
    notice = f"<div class='hook'>{_e(NOTICE['text'])}</div><br>" if NOTICE["text"] else ""
    NOTICE["text"] = ""
    return _page(c["name"], notice + info + mail + design + f"<div class='card'><h2>Befunde</h2><ul>{found}</ul></div>")


def run_job(settings: Settings, ort: str, branche: str, max_queries: int, design: bool,
            llm_factory: Callable[[], LLMClient | None]) -> None:
    """Kompletter Lauf im Hintergrund; Ausgaben landen in JOB["lines"]."""
    from main import apply_overrides, cmd_export, cmd_run

    class Lines:
        def write(self, text):
            JOB["lines"].extend(t for t in text.splitlines() if t.strip())

        def flush(self):
            pass

    JOB.update(running=True, lines=[])
    db = Database(settings.database_path)
    try:
        with redirect_stdout(Lines()):
            niche = apply_overrides(load_niche(), ort or None, branche or None)
            args = SimpleNamespace(skip_find=False, lead=None, retry_failed=False, no_browser=False, no_ai=False,
                                   max_queries=max_queries, datei=None)
            cmd_run(db, settings, niche, args)
            if design:
                rows = db.conn.execute("SELECT name, website FROM companies WHERE website LIKE 'http%'").fetchall()
                todo = [r for r in rows if load_review(settings.data_dir, domain_for(r["website"])) is None]
                print(f"== Design-Prüfung: {len(todo)} Websites")
                for r in todo:
                    review = asyncio.run(review_site(r["website"], settings.data_dir, llm_factory()))
                    print(f"   - {r['name']}: {review.summary}" + (f" ({review.error})" if review.error else ""))
                cmd_export(db, settings, niche, args)
            print("Fertig.")
    except Exception as exc:              # Fehler sichtbar machen statt den Server zu stoppen
        JOB["lines"].append(f"Abgebrochen: {type(exc).__name__}: {exc}")
    finally:
        db.close()
        JOB["running"] = False


def run_scan(settings: Settings, llm_factory: Callable[[], LLMClient | None]) -> None:
    """Design-Prüfung für alle Firmen, die ich noch manuell prüfen muss (ohne Bericht). Im Hintergrund."""
    JOB.update(running=True, lines=[])
    db = Database(settings.database_path)
    try:
        rows = [r for r in collect_rows(db, settings.data_dir)[0]
                if r[COL["Manuell prüfen"]] == "Ja" and (r[COL["Website"]] or "").startswith("http")
                and load_review(settings.data_dir, domain_for(r[COL["Website"]])) is None]
        JOB["lines"].append(f"Design-Prüfung für {len(rows)} Websites …")
        for r in rows:
            review = asyncio.run(review_site(r[COL["Website"]], settings.data_dir, llm_factory()))
            JOB["lines"].append(f"{r[COL['Firma']]}: {review.summary}" + (f" ({review.error})" if review.error else ""))
        JOB["lines"].append("Fertig.")
    except Exception as exc:
        JOB["lines"].append(f"Abgebrochen: {type(exc).__name__}: {exc}")
    finally:
        db.close()
        JOB["running"] = False


def render_job() -> bytes:
    refresh = "<meta http-equiv='refresh' content='3'>" if JOB["running"] else ""
    state = "läuft …" if JOB["running"] else "fertig"
    return _page("Suchlauf", f"{refresh}<div class='card'><div class='muted'><a href='/'>← Übersicht</a></div>"
                             f"<h1>Suchlauf {state}</h1><pre>{_e(chr(10).join(JOB['lines']) or 'startet …')}</pre></div>")


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
            if parts.path == "/firma":
                db = Database(settings.database_path)
                try:
                    return self._send(200, render_company(db, settings, int(q.get("id", 0))))
                finally:
                    db.close()
            if parts.path == "/lauf":
                return self._send(200, render_job())
            if parts.path == "/bericht":
                review = load_review(settings.data_dir, q.get("d"))
                if review is None:
                    return self._send(404, _page("Nicht gefunden", "<div class='card'>Kein Bericht. "
                                                                   "<a href='/'>Zur Übersicht</a></div>"))
                db = Database(settings.database_path)
                try:
                    return self._send(200, render_report(review, render_mail_card(db, settings, review)))
                finally:
                    db.close()
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
            path = urlsplit(self.path).path
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            if path == "/lauf":
                if not JOB["running"]:
                    threading.Thread(target=run_job, daemon=True, args=(
                        settings, form.get("ort", "").strip(), form.get("branche", ""),
                        max(1, min(30, int(form.get("max") or 4))), "design" in form, llm_factory)).start()
                return self._send(303, headers={"Location": "/lauf"})
            if path == "/scan":
                if not JOB["running"]:
                    threading.Thread(target=run_scan, args=(settings, llm_factory), daemon=True).start()
                return self._send(303, headers={"Location": "/lauf"})
            if path == "/kontakte":
                from modules.contact_import import import_contacts
                db = Database(settings.database_path)
                try:
                    r = import_contacts(db, form.get("csv", ""))
                    NOTICE["text"] = (f"Kontakte abgeglichen: {r.imported} übernommen ({r.matched_existing} bekannte "
                                      f"Firmen, {r.created} neu)" + (f", {len(r.errors)} Fehler" if r.errors else ""))
                except Exception as exc:
                    NOTICE["text"] = f"Kontakte-Import fehlgeschlagen: {exc}"
                finally:
                    db.close()
                return self._send(303, headers={"Location": "/"})
            if path == "/bericht_entwurf":
                return self._report_draft(form)
            if path == "/postfach":
                from modules.mailbox import sync
                db = Database(settings.database_path)
                try:
                    r = sync(db, settings)
                    NOTICE["text"] = f"Abgleich: {len(r.sent)} neu gesendet, {len(r.replied)} neue Antworten."
                except Exception as exc:
                    NOTICE["text"] = f"Postfach-Abgleich fehlgeschlagen: {exc}"
                finally:
                    db.close()
                return self._send(303, headers={"Location": "/"})
            if path == "/firma":
                return self._company_action(form)
            if path != "/pruefen":
                return self._send(404)
            url = form.get("url", "").strip()
            if not url:
                return self._send(303, headers={"Location": "/"})
            if "://" not in url:
                url = "https://" + url
            review = asyncio.run(review_site(url, settings.data_dir, llm_factory()))
            return self._send(303, headers={"Location": f"/bericht?d={quote(review.domain)}"})

        def _report_draft(self, form: dict):
            from core.errors import SendNotAllowed
            from models.schemas import EmailDraft, Lead
            from modules.mailer import save_draft
            domain, url, cid = form.get("domain", ""), form.get("url", ""), None
            db = Database(settings.database_path)
            try:
                row = db.conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
                cid = row["id"] if row else db.upsert_company(Lead(name=domain, website=url, source="manual",
                                                                    source_ref=domain)).company_id
                draft = EmailDraft(recipient=form.get("empfaenger", "").strip(), subject=form.get("betreff", ""),
                                   body=form.get("text", ""), finding_ids=["design_review"])
                save_draft(db, cid, draft, settings)
                NOTICE["text"] = "Entwurf gespeichert."
            except SendNotAllowed as exc:
                NOTICE["text"] = f"Nicht gespeichert: {exc}"
            except Exception as exc:
                NOTICE["text"] = f"Nicht gespeichert: {exc}"
                return self._send(303, headers={"Location": f"/bericht?d={quote(domain)}"})
            finally:
                db.close()
            return self._send(303, headers={"Location": f"/firma?id={cid}"})

        def _company_action(self, form: dict):
            from modules.mailer import build_message, save_draft
            from modules.pitch import build_design_draft, lint_draft
            from models.schemas import EmailDraft
            from services.outreach_service import choose_recipient
            cid, action = int(form.get("id", 0)), form.get("aktion", "")
            db = Database(settings.database_path)
            try:
                draft = db.get_draft(int(form["draft"])) if form.get("draft") else None
                if draft is not None and draft["company_id"] != cid:
                    return self._send(400)
                if action == "speichern" and draft:
                    with db.transaction() as c:
                        c.execute("UPDATE email_drafts SET subject = ?, body = ? WHERE id = ?",
                                  (form.get("betreff", draft["subject"]), form.get("text", draft["body"]), draft["id"]))
                elif action == "postfach" and draft:
                    from modules.mailbox import put_draft
                    msg = build_message(EmailDraft(recipient=draft["recipient"], subject=draft["subject"],
                                                   body=draft["body"], finding_ids=[]),
                                        settings.sender_name, settings.sender_address, draft["id"])
                    try:
                        NOTICE["text"] = f"Entwurf liegt im Postfach-Ordner „{put_draft(settings, msg)}“."
                    except Exception as exc:
                        NOTICE["text"] = f"Postfach nicht erreichbar: {exc}"
                elif action in MAIL_STATE and draft:
                    db.record_contact(cid, Channel.EMAIL, Outcome(action), recipient=draft["recipient"],
                                      draft_id=draft["id"], source="manual")
                    if action == "sent" and draft["status"] == "draft":
                        db.set_draft_status(draft["id"], "sent")
                elif action == "design_entwurf":
                    c = db.get_company(cid)
                    review = load_review(settings.data_dir, domain_for(c["website"]))
                    recipient = choose_recipient(db, cid)
                    if review and review.hook and recipient:
                        sender = _sender(settings)
                        new = build_design_draft(review.domain, review.hook, recipient, sender)
                        # Zahlen stammen aus dem Design-Urteil (z. B. „14 px“), nicht erfunden
                        problems = [x for x in lint_draft(new, [], sender) if not x.startswith("unbelegte Zahl")]
                        if problems:
                            NOTICE["text"] = "Entwurf verworfen: " + "; ".join(problems)
                        else:
                            save_draft(db, cid, new, settings)
                    else:
                        NOTICE["text"] = "Kein Entwurf möglich: keine E-Mail-Adresse oder kein Aufhänger."
            except Exception as exc:
                NOTICE["text"] = f"Fehler: {exc}"
            finally:
                db.close()
            return self._send(303, headers={"Location": f"/firma?id={cid}"})

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
