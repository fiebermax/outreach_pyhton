"""Zentrale Steuerung (Kommandozeile).

Beispiele:
    python main.py run                          # fragt Ort und Branche ab, dann alle Schritte
    python main.py run --ort Burgau --branche Maler --max-queries 4
    python main.py status
    python main.py drafts
    python main.py retry --lead 12 --step crawl
    python main.py contacts import docs/kontakte_vorlage.csv
    python main.py contact --lead 12 --kanal telefon --ergebnis kein_interesse --notiz "will nicht"
    python main.py block --email info@firma.de --grund "Widerspruch"

Versand gibt es nur als Einzelbefehl und nur mit Freigabe:
    python main.py approve --lead 12 --basis einwilligung --notiz "..." --von "Maximilian Fieber"
    python main.py send --draft 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from config import Settings, get_settings
from core.errors import MissingCredentialError, OutreachError, SendNotAllowed
from core.logging_setup import setup_from_settings
from core.niche import NicheProfile, load_niche
from modules.database import Channel, Database, Outcome, Step
from modules.pitch import Sender

log = logging.getLogger("main")

CHANNELS = {"email": Channel.EMAIL, "telefon": Channel.PHONE, "besuch": Channel.VISIT, "brief": Channel.LETTER,
            "social": Channel.SOCIAL, "sonstiges": Channel.OTHER}
OUTCOMES = {"gesendet": Outcome.SENT, "keine_antwort": Outcome.NO_REPLY, "antwort": Outcome.REPLIED,
            "interessiert": Outcome.INTERESTED, "kein_interesse": Outcome.NOT_INTERESTED, "termin": Outcome.MEETING,
            "kunde": Outcome.CUSTOMER, "unzustellbar": Outcome.BOUNCED, "sperren": Outcome.DO_NOT_CONTACT}


def apply_overrides(niche: NicheProfile, ort: str | None, branche: str | None) -> NicheProfile:
    """Ort/Branche aus der Kommandozeile schränken das Nischenprofil für diesen Lauf ein."""
    data = niche.model_dump()
    if ort:
        data["region"]["ort"] = ort
        data["region"]["weitere_orte"] = []
    if branche:
        wanted = branche.lower()
        selected = [b for b in data["branche"]
                    if wanted in b["name"].lower() or any(wanted in w.lower() for w in b["suchbegriffe"])]
        if not selected:
            names = ", ".join(b["name"] for b in data["branche"])
            raise SystemExit(f"Branche „{branche}“ nicht in niche.toml. Vorhanden: {names}")
        data["branche"] = selected
    return NicheProfile.model_validate(data)


def make_sender(settings: Settings, niche: NicheProfile) -> Sender:
    return Sender(name=settings.sender_name, region=niche.region.ort, email=settings.sender_address)


def make_llm(settings: Settings):
    if settings.deepseek_api_key is None:
        log.warning("DEEPSEEK_API_KEY fehlt – Analyse nur mit deterministischen Prüfungen")
        return None
    from modules.llm import CostTracker, DeepSeekClient
    return DeepSeekClient(settings.require_secret("deepseek_api_key"), model=settings.deepseek_model,
                          base_url=settings.deepseek_base_url, tracker=CostTracker(settings.ai_max_cost_eur),
                          price_input_per_mtok_eur=settings.deepseek_price_input_per_mtok_eur,
                          price_output_per_mtok_eur=settings.deepseek_price_output_per_mtok_eur)


def print_report(title: str, report) -> None:
    print(f"\n== {title}: {report.done} erledigt, {report.skipped} übersprungen, {report.failed} fehlgeschlagen")
    for line in report.details:
        print(f"   - {line}")


# --------------------------------------------------------------------------- Befehle

def cmd_find(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    from modules.lead_finder import GooglePlacesClient, PlacesConfig, find_leads
    client = GooglePlacesClient(settings.require_secret("google_places_api_key"), db, PlacesConfig(
        max_requests_per_run=settings.places_max_requests_per_run, monthly_limit=settings.places_monthly_limit,
        cache_days=settings.places_cache_days))
    r = find_leads(db, client, niche, max_queries=args.max_queries)
    print(f"\n== Lead-Suche: {r.queries} Suchanfragen ({r.api_requests} API-Abfragen, {r.cache_hits} aus Cache), "
          f"{r.results} Treffer, {r.created} neu, {r.updated} aktualisiert")
    print(f"   Branchen: {dict(r.by_branch)} · Website: {dict(r.website_kinds)} · übersprungen: {dict(r.skipped)}")
    for e in r.errors:
        print(f"   ! {e}")
    if r.stopped_reason:
        print(f"   Abbruch: {r.stopped_reason}")


def cmd_run(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    from services.analysis_service import run_analysis
    from services.crawl_service import run_crawls
    from services.outreach_service import run_scoring_and_drafts
    ids = [args.lead] if getattr(args, "lead", None) else None
    if not args.skip_find:
        cmd_find(db, settings, niche, args)
    print_report("Crawl", asyncio.run(run_crawls(db, settings, company_ids=ids, retry_failed=args.retry_failed,
                                                 use_browser=not args.no_browser)))
    print_report("Analyse", asyncio.run(run_analysis(db, settings, None if args.no_ai else make_llm(settings),
                                                     company_ids=ids, retry_failed=args.retry_failed)))
    scored, drafted = run_scoring_and_drafts(db, settings, niche, make_sender(settings, niche), company_ids=ids)
    print_report("Bewertung", scored)
    print_report("Entwürfe", drafted)
    print(f"\nEntwürfe liegen in {settings.data_dir / 'drafts'} – es wurde nichts versendet.")


def cmd_status(db: Database, *_args) -> None:
    c = db.conn
    total = c.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"Unternehmen: {total}")
    for row in c.execute("SELECT website_kind, COUNT(*) n FROM companies GROUP BY website_kind"):
        print(f"   Website {row['website_kind']}: {row['n']}")
    for step in Step:
        rows = c.execute("SELECT status, COUNT(*) n FROM job_steps WHERE step = ? GROUP BY status", (step.value,)).fetchall()
        if rows:
            print(f"Schritt {step.value}: " + ", ".join(f"{r['status']} {r['n']}" for r in rows))
    drafts = c.execute("SELECT status, COUNT(*) n FROM email_drafts GROUP BY status").fetchall()
    print("Entwürfe: " + (", ".join(f"{r['status']} {r['n']}" for r in drafts) or "keine"))
    print(f"Places-Abfragen diesen Monat: {db.api_calls_this_month('google_places')}")
    errors = c.execute("SELECT COUNT(*) FROM error_log").fetchone()[0]
    print(f"Fehlerprotokoll: {errors} Einträge")


def cmd_drafts(db: Database, settings: Settings, *_args) -> None:
    rows = db.list_drafts()
    if not rows:
        print("Keine Entwürfe.")
    for r in rows:
        print(f"#{r['id']:<4} {r['status']:<8} {r['company_name'][:35]:<35} → {r['recipient']:<32} {r['subject']}")
        print(f"       Datei: {settings.data_dir / r['eml_path'] if r['eml_path'] else '-'}")


def cmd_leads(db: Database, *_args) -> None:
    rows = db.conn.execute(
        """SELECT c.id, c.name, c.niche_branch, c.website_kind, c.website,
                  (SELECT score FROM lead_scores s WHERE s.company_id = c.id ORDER BY id DESC LIMIT 1) AS score,
                  (SELECT status FROM job_steps j WHERE j.company_id = c.id AND j.step = 'draft') AS draft
           FROM companies c ORDER BY score DESC NULLS LAST, c.id""").fetchall()
    for r in rows:
        score = "–" if r["score"] is None else f"{r['score']:.0f}"
        print(f"#{r['id']:<4} {score:>4}  {r['name'][:38]:<38} {r['niche_branch'] or '–':<20} "
              f"{r['website_kind'] or '–':<9} {r['draft'] or ''}")


def cmd_retry(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    steps = [Step(args.step)] if args.step != "all" else list(Step)
    for step in steps:
        db.reset_step(args.lead, step)
    print(f"Unternehmen {args.lead}: {', '.join(s.value for s in steps)} zurückgesetzt. Jetzt z. B.: "
          f"python main.py run --skip-find --lead {args.lead}")


def cmd_contacts(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    if args.action == "import":
        from modules.contact_import import import_contacts
        r = import_contacts(db, Path(args.file))
        print(f"{r.imported} importiert ({r.matched_existing} bekannt, {r.created} neu angelegt)")
        for e in r.errors:
            print(f"   ! {e}")
    else:
        for r in db.contact_overview():
            print(f"#{r['id']:<4} {r['name'][:35]:<35} zuletzt {r['last_contact'][:10]} per {r['channel']:<6} "
                  f"→ {r['outcome']:<15} ({r['contacts']}×){'  GESPERRT' if r['do_not_contact'] else ''}")


def cmd_contact(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    db.record_contact(args.lead, CHANNELS[args.kanal], OUTCOMES[args.ergebnis], recipient=args.email,
                      note=args.notiz, source="manual")
    print(f"Kontakt für Unternehmen {args.lead} gespeichert. Prüfung: {db.may_contact(args.lead).reason}")


def cmd_block(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    if args.email:
        db.add_block("email", args.email, args.grund)
    if args.domain:
        db.add_block("domain", args.domain, args.grund)
    print("Gesperrt.")


def cmd_approve(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    from modules.mailer import approve_sending
    approve_sending(db, args.lead, args.basis, args.notiz, args.von)
    print(f"Versandfreigabe für Unternehmen {args.lead} dokumentiert ({args.basis}).")


def cmd_send(db: Database, settings: Settings, niche: NicheProfile, args) -> None:
    from modules.mailer import send_draft
    send_draft(db, args.draft, settings)
    print(f"Entwurf {args.draft} versendet.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="B2B-Outreach: Leads finden, Websites prüfen, Entwürfe erstellen.")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="alle Schritte: suchen, crawlen, analysieren, bewerten, Entwürfe")
    run.add_argument("--ort"), run.add_argument("--branche")
    run.add_argument("--max-queries", type=int, default=None, help="Places-Suchanfragen begrenzen")
    run.add_argument("--skip-find", action="store_true", help="keine neue Suche, nur vorhandene Leads verarbeiten")
    run.add_argument("--retry-failed", action="store_true", help="fehlgeschlagene Schritte erneut versuchen")
    run.add_argument("--no-ai", action="store_true", help="ohne KI-Analyse")
    run.add_argument("--no-browser", action="store_true", help="ohne Playwright (schneller, ungenauer)")
    run.add_argument("--lead", type=int, help="nur dieses Unternehmen")
    run.add_argument("--yes", action="store_true", help="nicht interaktiv nachfragen")

    find = sub.add_parser("find", help="nur Lead-Suche")
    find.add_argument("--ort"), find.add_argument("--branche")
    find.add_argument("--max-queries", type=int, default=None)

    sub.add_parser("status", help="Übersicht")
    sub.add_parser("leads", help="Unternehmen mit Score")
    sub.add_parser("drafts", help="Entwürfe anzeigen")

    retry = sub.add_parser("retry", help="Schritt für ein Unternehmen zurücksetzen")
    retry.add_argument("--lead", type=int, required=True)
    retry.add_argument("--step", choices=[s.value for s in Step] + ["all"], required=True)

    contacts = sub.add_parser("contacts", help="Kontakthistorie")
    contacts.add_argument("action", choices=["list", "import"])
    contacts.add_argument("file", nargs="?")

    contact = sub.add_parser("contact", help="einen Kontakt erfassen")
    contact.add_argument("--lead", type=int, required=True)
    contact.add_argument("--kanal", choices=list(CHANNELS), required=True)
    contact.add_argument("--ergebnis", choices=list(OUTCOMES), required=True)
    contact.add_argument("--email"), contact.add_argument("--notiz")

    block = sub.add_parser("block", help="E-Mail oder Domain sperren")
    block.add_argument("--email"), block.add_argument("--domain"), block.add_argument("--grund")

    approve = sub.add_parser("approve", help="Versandfreigabe dokumentieren (Rechtsgrundlage nötig)")
    approve.add_argument("--lead", type=int, required=True)
    approve.add_argument("--basis", choices=["einwilligung", "bestandskunde", "anfrage_des_empfaengers"], required=True)
    approve.add_argument("--notiz", required=True), approve.add_argument("--von", required=True)

    send = sub.add_parser("send", help="einen Entwurf versenden (nur wenn freigegeben und aktiviert)")
    send.add_argument("--draft", type=int, required=True)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None, niche: NicheProfile | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    setup_from_settings(settings)
    niche = niche or load_niche()
    if args.command in ("run", "find"):
        if args.command == "run" and not args.ort and not args.yes and sys.stdin.isatty():
            args.ort = input(f"Ort [{niche.region.ort}]: ").strip() or None
            args.branche = input("Branche (leer = alle aus niche.toml): ").strip() or None
        niche = apply_overrides(niche, args.ort, args.branche)
    db = Database(settings.database_path)
    handlers = {"run": cmd_run, "find": cmd_find, "status": cmd_status, "leads": cmd_leads, "drafts": cmd_drafts,
                "retry": cmd_retry, "contacts": cmd_contacts, "contact": cmd_contact, "block": cmd_block,
                "approve": cmd_approve, "send": cmd_send}
    try:
        handlers[args.command](db, settings, niche, args)
    except (SendNotAllowed, MissingCredentialError) as exc:
        print(f"Abgebrochen: {exc}")
        return 2
    except (OutreachError, ValueError) as exc:
        print(f"Fehler: {exc}")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
