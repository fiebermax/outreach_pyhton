"""Modul F: Anti-Pitch-Entwürfe.

Aufbau: Begrüßung → eine konkrete Beobachtung → warum sie relevant sein könnte → kurzer Hinweis →
zurückhaltende Vorstellung → Abmeldehinweis.

Die Texte entstehen aus festen Bausteinen je Befundtyp, gefüllt nur mit Werten aus dem Befund
(Zitat, Messwert, Fundstelle). Dadurch kann keine Behauptung in die Mail gelangen, die nicht
belegt ist. `lint_draft` prüft jeden Entwurf zusätzlich.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from models.schemas import EmailDraft, Finding


@dataclass(frozen=True)
class Sender:
    name: str = "Maximilian Fieber"
    tagline: str = "Webdesign & digitale Lösungen"
    region: str = "Jettingen-Scheppach"
    email: str | None = None
    phone: str | None = None
    website: str | None = "fieberki.de"


@dataclass(frozen=True)
class Block:
    subject: str
    observation: str
    why: str
    hint: str


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or url).removeprefix("www.")


def _path(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return "auf Ihrer Startseite" if not path else f"auf der Seite {path}"


def _sentence_start(text: str) -> str:
    """Nur den ersten Buchstaben groß – str.capitalize() würde den Rest kleinschreiben ("Auf der seite …")."""
    return text[:1].upper() + text[1:]


def block_for(f: Finding) -> Block | None:
    """Textbaustein für einen Befund; None = Befundtyp eignet sich nicht für einen Anti-Pitch."""
    d, m = _domain(f.source_url), f.measurement or {}
    issue = f.issue_id
    if issue == "phone_not_clickable":
        number = str(m.get("number") or re.sub(r"^.*?„|“.*$|\s*\(kein tel:-Link\)", "", f.evidence)).strip()
        return Block(f"Telefonnummer auf {d}",
                     f"{_sentence_start(_path(f.source_url))} steht Ihre Telefonnummer nur als Text "
                     f"(„{number[:60]}“). Auf dem Handy lässt sie sich deshalb nicht antippen.",
                     "Wer die Seite auf dem Handy öffnet, muss die Nummer abtippen.",
                     "Mit einem Telefon-Link (tel:) ist das in ein paar Minuten behoben.")
    if issue == "tel_link_malformed":
        return Block(f"Telefon-Link auf {d}",
                     f"der Telefon-Link {_path(f.source_url)} ist im Code fehlerhaft hinterlegt ({f.evidence}).",
                     "Beim Antippen kann dadurch eine falsche oder gar keine Nummer gewählt werden.",
                     "Der Link müsste nur auf die reine Nummer korrigiert werden.")
    if issue == "horizontal_overflow_mobile":
        culprit = m.get("culprit")
        return Block(f"Darstellung von {d} auf dem Handy",
                     "auf dem Smartphone lässt sich Ihre Startseite seitlich verschieben, weil ein Element breiter "
                     "ist als der Bildschirm" + (f" (im Code: {culprit})" if culprit and culprit != "unbekannt" else "") + ".",
                     "Beim Scrollen verrutscht die Seite dadurch zur Seite.",
                     "Das lässt sich an diesem einen Element beheben.")
    if issue == "viewport_meta_missing":
        return Block(f"{d} auf dem Smartphone",
                     "Ihre Startseite wird auf Smartphones verkleinert wie am Computer angezeigt; es fehlt die "
                     "technische Angabe für mobile Geräte (Viewport).",
                     "Besucher müssen dann zoomen, um etwas lesen zu können.",
                     "Die Angabe allein behebt es nicht ganz, das Layout muss auch fürs Handy passen.")
    if issue == "no_https":
        return Block(f"„Nicht sicher“ bei {d}",
                     "Ihre Website wird unverschlüsselt (http) aufgerufen. Browser wie Chrome zeigen deshalb "
                     "„Nicht sicher“ in der Adresszeile.",
                     "Das sehen Besucher direkt neben Ihrer Adresse.",
                     "Ein Zertifikat lässt sich beim Hoster einrichten, häufig ohne Zusatzkosten.")
    if issue == "http_not_redirected":
        return Block(f"Weiterleitung bei {d}",
                     f"Ihre Website ist auch unverschlüsselt unter {m.get('url', 'http://' + d)} erreichbar und "
                     "leitet dort nicht auf die sichere https-Version weiter.",
                     "Wer die Adresse ohne https eintippt, sieht im Browser „Nicht sicher“.",
                     "Eine Weiterleitung auf https lässt sich beim Hoster einstellen.")
    if issue == "ssl_certificate_invalid":
        return Block(f"Sicherheitswarnung bei {d}",
                     "beim Aufruf Ihrer Website zeigt der Browser eine Sicherheitswarnung, weil das Zertifikat "
                     "nicht akzeptiert wird.",
                     "Besucher kommen erst nach einem zusätzlichen Klick auf „Trotzdem fortfahren“ auf die Seite.",
                     "Das Zertifikat muss erneuert oder korrekt eingerichtet werden.")
    if issue == "copyright_outdated":
        return Block(f"Jahreszahl auf {d}",
                     f"im Footer Ihrer Website steht noch „{f.evidence}“.",
                     "Das kann den Eindruck erwecken, die Seite werde nicht mehr gepflegt.",
                     "Die Jahreszahl lässt sich auch automatisch aktualisieren.")
    if issue in ("title_missing", "title_generic"):
        title = re.sub(r"</?title>", "", f.evidence)
        return Block(f"Ihr Eintrag bei Google ({d})",
                     "in Google und im Browser-Tab erscheint Ihre Startseite " +
                     ("ohne eigenen Titel." if issue == "title_missing" else f"nur mit dem Titel „{title}“."),
                     "Dort steht normalerweise, was Sie anbieten und wo, damit man Sie in der Trefferliste erkennt.",
                     "Der Titel lässt sich im Seiten-Editor ändern.")
    if issue == "broken_internal_links":
        first = f.evidence.split(";")[0]
        return Block(f"Toter Link auf {d}",
                     f"auf Ihrer Website führt ein Link ins Leere: {first}.",
                     "Besucher landen dort auf einer Fehlerseite.",
                     "")
    if issue == "homepage_almost_no_text":
        return Block(f"Startseite von {d}",
                     f"Ihre Startseite enthält kaum lesbaren Text, nur {m.get('words')} Wörter.",
                     "Besucher und Suchmaschinen erfahren dort wenig darüber, was Sie anbieten.",
                     "Schon ein kurzer Absatz zu Leistungen und Einzugsgebiet hilft.")
    return None


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def build_design_draft(domain: str, paragraph: str, recipient: str, sender: Sender,
                       extra: str | None = None) -> EmailDraft:
    """Entwurf aus dem Design-Urteil (höflicher Absatz der Bild-KI), optional plus ein technischer Punkt."""
    paragraph = paragraph.strip()
    if paragraph[-1:] not in ".!?":
        paragraph += "."
    extras = [extra] if extra else []
    return EmailDraft(recipient=recipient, subject=f"Ihre Website {domain}",
                      body=_compose(domain, _sentence_start(paragraph), "", extras, sender, lead_in=False),
                      finding_ids=["design_review"])


def _compose(domain: str, first: str, hint: str, extras: list[str], sender: Sender, lead_in: bool = True) -> str:
    """Ohne Selbstvorstellung: gleich zur Sache, Kontakt und Website in der Signatur."""
    lines = [
        "Guten Tag,",
        "",
        f"beim Durchsehen von Websites aus der Region bin ich auf {domain} gestoßen."
        + (" Dabei ist mir Folgendes aufgefallen:" if lead_in else ""),
        "",
        first,
    ]
    if hint:
        lines += ["", hint]
    for extra in extras:
        lines += ["", f"Außerdem: {extra}"]
    closing = "Wenn Sie das ändern möchten, unterstütze ich Sie gern – eine kurze Antwort auf diese Mail genügt."
    if sender.website:
        closing += f" Beispiele meiner Arbeit finden Sie auf {sender.website}."
    lines += ["", closing, "", "Viele Grüße", sender.name, sender.tagline]
    lines += [x for x in (sender.phone, sender.email, sender.website) if x]
    lines += ["", "Falls Sie keine weiteren Nachrichten von mir möchten, genügt eine kurze Antwort."]
    return "\n".join(lines)


def build_draft(findings: list[Finding], recipient: str, sender: Sender) -> EmailDraft | None:
    """Entwurf aus höchstens zwei Befunden. None, wenn kein Befund einen Baustein hat."""
    blocks = [(f, b) for f in findings if (b := block_for(f)) is not None][:2]
    if not blocks:
        return None
    (first, b1), rest = blocks[0], blocks[1:]
    extras = [b.observation[0].lower() + b.observation[1:] for _, b in rest]
    body = _compose(_domain(first.source_url), _join(_sentence_start(b1.observation), b1.why), b1.hint, extras, sender)
    return EmailDraft(recipient=recipient, subject=b1.subject, body=body,
                      finding_ids=[f.issue_id for f, _ in blocks])


# --------------------------------------------------------------------------- Prüfung

FORBIDDEN = [
    "unlock", "elevate", "seamless", "revolution", "cutting-edge", "empower", "nahtlos", "bahnbrechend",
    "einzigartig", "innovativ", "garantiert", "garantie", "sofort", "dringend", "nur heute", "jetzt zuschlagen",
    "exklusiv", "maßgeschneidert", "ganzheitlich", "auf das nächste level", "game changer", "umsatz", "conversion",
    "verlieren sie", "kunden verlieren", "ich habe ihre website getestet", "ki-analyse", "künstliche intelligenz",
]
# Pauschalbehauptungen über Besucherverhalten sind nicht belegbar.
SWEEPING = re.compile(r"\b(die meisten|meistens|meist|oft|immer|niemals|jeder kunde|alle kunden|studien|statistisch)\b",
                      re.I)


def lint_draft(draft: EmailDraft, findings: list[Finding], sender: Sender) -> list[str]:
    """Liefert Verstöße; leere Liste = Entwurf darf gespeichert werden."""
    problems = []
    text = draft.subject + "\n" + draft.body
    low = text.lower()
    for word in FORBIDDEN:
        if word in low:
            problems.append(f"verbotene Formulierung: „{word}“")
    for match in SWEEPING.finditer(draft.body):
        problems.append(f"Pauschalbehauptung: „{match.group(0)}“")
    if text.count("!") > 1:
        problems.append("mehr als ein Ausrufezeichen")
    if len(draft.body) > 1400:
        problems.append(f"zu lang ({len(draft.body)} Zeichen)")
    if len(draft.finding_ids) > 2:
        problems.append("mehr als zwei Befunde")
    if "keine weiteren Nachrichten" not in draft.body:
        problems.append("Abmeldehinweis fehlt")
    # Jede Zahl im Text muss aus einem Befund oder der Signatur stammen.
    allowed_source = " ".join([f.evidence + " " + f.source_url + " " + str(f.measurement or "") for f in findings]
                              + [sender.phone or "", sender.email or "", sender.website or "", sender.region])
    allowed = set(re.findall(r"\d+", allowed_source))
    for number in set(re.findall(r"\d+", text)):
        if number not in allowed:
            problems.append(f"unbelegte Zahl im Text: {number}")
    return problems
