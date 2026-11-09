#!/usr/bin/env python3
# FTM - Monitoring
# Supervision des environnements Secure Transport (QUA, DEV, INT)
#
# python supervision.py            -> vraies machines
# python supervision.py --demo     -> machines fictives
# python supervision.py --test-mail

import argparse
import csv
import json
import os
import random
import re
import smtplib
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fichiers
DOSSIER = os.path.dirname(os.path.abspath(__file__))
FICHIER_CSV = os.path.join(DOSSIER, "environnements.csv")
FICHIER_EXEMPLE = os.path.join(DOSSIER, "environnements.exemple.csv")
FICHIER_SERVICES = os.path.join(DOSSIER, "services.csv")
FICHIER_PAGE = os.path.join(DOSSIER, "templates", "index.html")
FICHIER_ETAT = os.path.join(DOSSIER, "etat.json")
FICHIER_ALERTES = os.path.join(DOSSIER, "alertes.csv")
FICHIER_LOGO_MAIL = os.path.join(DOSSIER, "templates", "mail", "logo.png")

# Réglages
DEMO = False
TIMEOUT = 10
DELAI_SSH = 45
TAILLE_HISTO = 40
TAILLE_JOURNAL = 200
JOURS_DISPO = 7
CONFIRMATION = 2   # nb de contrôles avant d'envoyer une alerte

NOMS_ENV = {"QUA": "Qualification", "DEV": "Développement", "INT": "Intégration",
            "REC": "Recette", "PPR": "Pré-production", "PRD": "Production"}


def trouver_csv():
    return FICHIER_CSV if os.path.isfile(FICHIER_CSV) else None


def trouver_page():
    return FICHIER_PAGE if os.path.isfile(FICHIER_PAGE) else None


def lire_csv(chemin):
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f, delimiter=";")]


def lire_inventaire():
    # une ligne par machine x une ligne par service = liste des contrôles
    chemin = FICHIER_EXEMPLE if DEMO else (trouver_csv() or FICHIER_EXEMPLE)
    machines = [m for m in lire_csv(chemin) if m.get("env") and m.get("ip")]
    services = lire_csv(FICHIER_SERVICES)
    lignes = []
    for m in machines:
        for s in services:
            if s.get("type") != m.get("type") or not s.get("service"):
                continue
            ligne = dict(m)
            ligne["service"] = s["service"]
            ligne["port"] = s.get("port", "")
            ligne["commande_ssh"] = s.get("commande_ssh", "")
            ligne["categorie"] = s.get("categorie") or "service"
            ligne["url"] = m.get("url", "") if s.get("lien", "").lower() == "oui" else ""
            lignes.append(ligne)
    return lignes


def lire_reglages():
    if not os.path.isfile(FICHIER_ALERTES):
        return {}
    with open(FICHIER_ALERTES, encoding="utf-8-sig", newline="") as f:
        return {(r.get("parametre") or "").strip(): (r.get("valeur") or "").strip()
                for r in csv.DictReader(f, delimiter=";")}


def nb_confirmation():
    try:
        return max(1, int(lire_reglages().get("confirmation") or CONFIRMATION))
    except ValueError:
        return CONFIRMATION


def heure_rapport():
    valeur = lire_reglages().get("rapport_quotidien", "").strip()
    m = re.fullmatch(r"(\d{1,2})[:hH](\d{2})", valeur)
    return (int(m.group(1)), int(m.group(2))) if m else None


def lire_alertes():
    reglages = lire_reglages()
    if not reglages:
        return None
    if reglages.get("actif", "").lower() not in ("oui", "o", "yes", "1"):
        return None
    reglages["destinataires"] = [a.strip() for a in reglages.get("destinataires", "").split(",") if a.strip()]
    return reglages


def html_mail(changements, adresse_page, demo):
    arrets = [c for c in changements if not c["ok"]]
    reprises = [c for c in changements if c["ok"]]
    e = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    F = "font-family:Inter,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    envs = sorted({c["env"] for c in (arrets or reprises)})
    noms = " et ".join(", ".join(NOMS_ENV.get(c, c) for c in envs).rsplit(", ", 1))
    heure = changements[0]["heure"]

    sur = ("l'environnement " if len(envs) == 1 else "les environnements ") + noms
    if arrets:
        quoi = "l'arrêt d'un service" if len(arrets) == 1 else "l'arrêt de %d services" % len(arrets)
    else:
        quoi = "le redémarrage d'un service" if len(reprises) == 1 else "le redémarrage de %d services" % len(reprises)
    intro = "FTM - Monitoring a détecté %s sur %s lors du contrôle de %s." % (quoi, sur, heure)
    suite = "Les équipes concernées peuvent consulter le détail du nœud et l'historique des contrôles sur la page de supervision."

    def ligne(c, premiere):
        coul, lib = ("#e11d48", "Arrêté") if not c["ok"] else ("#059669", "Redémarré")
        bord = "" if premiere else "border-top:1px solid #e7eaef;"
        return ('<tr><td style="%s%spadding:13px 18px;font-size:14px;color:#0f172a">'
                '<span style="color:%s">&#9679;</span>&nbsp; <b style="font-weight:600">%s</b>'
                '<div style="%sfont-size:12px;color:#7d889a;padding:3px 0 0 18px">%s · %s · %s</div></td>'
                '<td align="right" style="%s%spadding:13px 18px;font-size:13px;font-weight:600;color:%s;white-space:nowrap;vertical-align:top">%s</td></tr>'
                % (bord, F, coul, e(c["service"]), F, e(c["env"]), e(c["noeud"]), e(c["hostname"]), bord, F, coul, lib))

    tous = arrets + reprises
    tableau = "".join(ligne(c, i == 0) for i, c in enumerate(tous))
    cid = "fond-alerte" if arrets else "fond-retabli"
    demo_txt = ('&nbsp;<span style="display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:#eef1f5;'
                'font-size:11px;font-weight:600;color:#7d889a;vertical-align:1px">Démo</span>') if demo else ""

    return gabarit_mail({
        "apercu": e(intro), "F": F, "cid": cid, "cid_mso": cid.replace("fond", "banniere"),
        "t1": "Alerte de" if arrets else "Services", "t2": "supervision" if arrets else "rétablis",
        "pt": "#f43f5e" if arrets else "#34d399", "pill": "Service arrêté" if len(arrets) == 1 else ("%d services arrêtés" % len(arrets) if arrets else "Services redémarrés"),
        "intro": e(intro), "titre_tableau": "Services concernés", "extra": "",
        "suite": e(suite), "tableau": tableau, "url": e(adresse_page), "demo": demo_txt})


def gabarit_mail(v):
    # même gabarit pour l'alerte et le rapport (les balises mso sont pour Outlook)
    return """<!doctype html><html lang="fr" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"><head><meta charset="utf-8">
<!--[if gte mso 9]><xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]--><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light only">
<style>@media only screen and (max-width:520px){.banniere{background-image:none!important;background-color:#101d4f!important}}</style></head>
<body style="margin:0;padding:0;background:#f1f4f9">
<div style="display:none;max-height:0;overflow:hidden">%(apercu)s</div>
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" bgcolor="#f1f4f9" style="background:#f1f4f9"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%%;max-width:600px;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 12px 32px -12px rgba(15,23,42,.18)">
 <tr><td style="font-size:0;line-height:0">
<!--[if mso]><img src="cid:%(cid_mso)s" width="600" height="260" alt="FTM - Monitoring : %(t1)s %(t2)s" style="display:block;border:0"><![endif]-->
<!--[if !mso]><!--><table role="presentation" width="100%%" cellpadding="0" cellspacing="0"><tr><td class="banniere" bgcolor="#101d4f" background="cid:%(cid)s" valign="top" height="260" style="background-color:#101d4f;background-image:url('cid:%(cid)s');background-size:cover;background-position:right center;background-repeat:no-repeat;height:260px;font-size:14px;line-height:1.4">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%%"><tr><td style="padding:34px 40px 0">
  <table role="presentation" cellpadding="0" cellspacing="0"><tr>
   <td style="vertical-align:middle"><img src="cid:logo-ftm" width="26" height="26" alt="" style="display:block;border-radius:7px"></td>
   <td style="vertical-align:middle;padding-left:9px;%(F)sfont-size:14px;font-weight:600;color:#ffffff">FTM - Monitoring</td>
  </tr></table>
  <p style="margin:0;padding-top:26px;%(F)sfont-size:11px;line-height:14px;mso-line-height-rule:exactly;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#93a5cf">Supervision &middot; Secure Transport</p>
  <p style="margin:0;padding-top:8px;%(F)sfont-size:34px;line-height:37px;mso-line-height-rule:exactly;font-weight:700;letter-spacing:-1px;color:#ffffff">%(t1)s<br><span style="color:#93c5fd">%(t2)s</span></p>
  <!--[if !mso]><!--><table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:16px"><tr><td style="border-radius:999px;background:#26356b;border:1px solid #3d4f8a;padding:7px 14px;%(F)sfont-size:12.5px;font-weight:500;color:#ffffff">
   <span style="color:%(pt)s;font-size:12px">&#9679;</span>&nbsp; %(pill)s</td></tr></table><!--<![endif]-->
  <!--[if mso]><p style="margin:0;padding-top:16px;font-family:'Segoe UI',Arial,sans-serif;font-size:13px;color:#ffffff"><span style="color:%(pt)s">&#9679;</span>&nbsp; %(pill)s</p><![endif]-->
  </td></tr></table>
 </td></tr></table><!--<![endif]-->
 </td></tr>
 <tr><td style="padding:34px 40px 0">
  <p style="%(F)smargin:0;font-size:15px;line-height:1.7;color:#0f172a;font-weight:600">Bonjour,</p>
  <p style="%(F)smargin:8px 0 0;font-size:15px;line-height:1.7;color:#465267">%(intro)s</p>
 </td></tr>
 <tr><td style="padding:22px 40px 0">
  <p style="%(F)smargin:0 0 10px 2px;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#7d889a">%(titre_tableau)s</p>
    <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e7eaef;border-radius:14px;border-collapse:separate">%(tableau)s</table>
 </td></tr>%(extra)s
 <tr><td style="padding:20px 40px 0">
  <p style="%(F)smargin:0;font-size:15px;line-height:1.7;color:#465267">%(suite)s</p>
 </td></tr>
 <tr><td style="padding:26px 40px 36px">
  <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="%(url)s" style="height:36px;v-text-anchor:middle;width:186px;" arcsize="50%%" stroke="f" fillcolor="#1d4ed8"><center style="color:#ffffff;font-family:'Segoe UI',Arial,sans-serif;font-size:13px;font-weight:bold;">Ouvrir la supervision &rarr;</center></v:roundrect><![endif]-->
  <!--[if !mso]><!--><table role="presentation" cellpadding="0" cellspacing="0"><tr><td bgcolor="#1d4ed8" style="border-radius:999px;background:#1d4ed8;background-image:linear-gradient(90deg,#2563eb 0%%,#1e40af 100%%)">
   <a href="%(url)s" style="%(F)sdisplay:inline-block;padding:9px 18px;font-size:13px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:999px">Ouvrir la supervision &nbsp;&rarr;</a>
  </td></tr></table><!--<![endif]-->
  </td></tr>
 <tr><td style="padding:0 40px"><div style="border-top:1px solid #e7eaef;font-size:0;line-height:0">&nbsp;</div></td></tr>
 <tr><td style="padding:28px 40px 36px">
  <table role="presentation" width="100%%" cellpadding="0" cellspacing="0"><tr>
   <td style="vertical-align:top;width:36px"><img src="cid:logo-ftm" width="28" height="28" alt="" style="display:block;border-radius:7px"></td>
   <td style="vertical-align:top;padding-left:10px">
    <div style="%(F)sfont-size:13px;line-height:18px;font-weight:700;color:#0f172a">FTM - Monitoring%(demo)s</div>
    <p style="%(F)smargin:6px 0 0;font-size:12px;line-height:1.6;color:#8a94a6">Message automatique, merci de ne pas y répondre.</p></td>
  </tr></table></td></tr>
</table></td></tr></table></body></html>""" % v


def preparer_mail(changements, reglages, adresse_page, demo=False):
    arrets = [c for c in changements if not c["ok"]]
    reprises = [c for c in changements if c["ok"]]
    envs = sorted({c["env"] for c in changements})
    pluriel = lambda n: "s" if n > 1 else ""
    parties = []
    if arrets:
        parties.append("%d service%s arrêté%s" % (len(arrets), pluriel(len(arrets)), pluriel(len(arrets))))
    if reprises:
        parties.append("%d redémarré%s" % (len(reprises), pluriel(len(reprises))))

    texte = []
    for titre, liste in (("Services arrêtés", arrets), ("Services redémarrés", reprises)):
        if liste:
            texte.append(titre + " :")
            texte += ["  - %s · %s (%s) · %s, à %s" % (c["env"], c["noeud"], c["hostname"], c["service"], c["heure"]) for c in liste]
            texte.append("")
    texte += ["Supervision : " + adresse_page, "",
              "Message automatique envoyé par FTM - Monitoring, merci de ne pas y répondre."]

    sujet = "%s[FTM - Monitoring] %s : %s" % ("[DÉMO] " if demo else "", ", ".join(envs), ", ".join(parties))
    return construire_message(sujet, "\n".join(texte), html_mail(changements, adresse_page, demo),
                              bool(arrets), reglages)


def construire_message(sujet, texte, corps_html, alerte, reglages):
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = "FTM - Monitoring <%s>" % (reglages.get("expediteur") or "ftm-monitoring@exemple.fr")
    msg["To"] = ", ".join(reglages.get("destinataires") or ["equipe@exemple.fr"])
    msg["Date"] = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(texte)
    msg.add_alternative(corps_html, subtype="html")
    html = msg.get_payload()[1]
    dossier_mail = os.path.dirname(FICHIER_LOGO_MAIL)
    fond = "fond-alerte" if alerte else "fond-retabli"
    images = [("logo.png", "png", "logo-ftm"), (fond + ".jpg", "jpeg", fond)]
    for nom, sous_type, cid in images:
        chemin = os.path.join(dossier_mail, nom)
        if os.path.isfile(chemin):
            with open(chemin, "rb") as f:
                html.add_related(f.read(), "image", sous_type, cid="<%s>" % cid)
    return msg


def preparer_rapport(donnees, journal, reglages, adresse_page, demo=False):
    e = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    F = "font-family:Inter,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    p = lambda n, mot: "%d %s%s" % (n, mot, "s" if n > 1 else "")
    pa = lambda n: "%d service%s arrêté%s" % (n, "s" if n > 1 else "", "s" if n > 1 else "")
    maintenant = datetime.now()
    depuis = maintenant - timedelta(hours=24)

    def date_evt(j):
        try:
            return datetime.strptime("%s %s" % (j.get("jour", ""), j.get("t", "")), "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    lignes_env, texte, arretes = [], [], []
    total_arrets_24h = 0
    for i, env in enumerate(donnees["envs"]):
        services = [s for n in env["nodes"] if not n.get("maintenance") for s in n["services"]]
        demarres = sum(1 for s in services if s["ok"] is True)
        dispos = [s["dispo7"] for s in services if s.get("dispo7") is not None]
        dispo = sum(dispos) / len(dispos) if dispos else None
        noeuds_ko = [n for n in env["nodes"] if not n.get("maintenance")
                     and any(s["ok"] is False for s in n["services"])]
        for n in env["nodes"]:
            for s in n["services"]:
                if s["ok"] is False and not n.get("maintenance"):
                    arretes.append((env["code"], n, s))
        actifs = len(env["nodes"]) - len(noeuds_ko)
        if not noeuds_ko:
            coul, etat = "#059669", "Opérationnel"
        elif actifs > 0:
            coul, etat = "#d97706", "Dégradé"
        else:
            coul, etat = "#e11d48", "Hors service"
        arrets_24h = sum(1 for j in journal.get(env["code"], [])
                         if j.get("type") != "maintenance" and not j.get("ok")
                         and (date_evt(j) or depuis) > depuis)
        total_arrets_24h += arrets_24h
        detail = "%d / %d services et accès OK · %s sur 24 h%s" % (
            demarres, len(services), p(arrets_24h, "arrêt"),
            (" · disponibilité 7 j %s %%" % ("%.1f" % dispo).replace(".", ",")) if dispo is not None else "")
        maint = [n["name"] for n in env["nodes"] if n.get("maintenance")]
        if maint:
            detail += " · en maintenance : " + ", ".join(maint)
        bord = "" if i == 0 else "border-top:1px solid #e7eaef;"
        lignes_env.append(
            '<tr><td style="%s%spadding:13px 18px;font-size:14px;color:#0f172a">'
            '<span style="color:%s">&#9679;</span>&nbsp; <b style="font-weight:600">%s</b> <span style="color:#7d889a">%s</span>'
            '<div style="%sfont-size:12px;color:#7d889a;padding:3px 0 0 18px">%s</div></td>'
            '<td align="right" style="%s%spadding:13px 18px;font-size:13px;font-weight:600;color:%s;white-space:nowrap;vertical-align:top">%s</td></tr>'
            % (bord, F, coul, e(env["name"]), e(env["code"]), F, e(detail), bord, F, coul, etat))
        texte.append("  - %s (%s) : %s, %s" % (env["name"], env["code"], etat, detail))

    extra = ""
    if arretes:
        lignes = "".join(
            '<tr><td style="%s%spadding:11px 18px;font-size:13.5px;color:#0f172a"><span style="color:#e11d48">&#9679;</span>&nbsp; '
            '<b style="font-weight:600">%s</b><div style="%sfont-size:12px;color:#7d889a;padding:3px 0 0 18px">%s · %s · %s · arrêté depuis %s</div></td></tr>'
            % ("" if k == 0 else "border-top:1px solid #e7eaef;", F, e(s["name"]), F, e(code), e(n["name"]), e(n["host"]), e(s["since"]))
            for k, (code, n, s) in enumerate(arretes[:12]))
        extra = ('<tr><td style="padding:22px 40px 0"><p style="%smargin:0 0 10px 2px;font-size:11px;font-weight:600;letter-spacing:2px;'
                 'text-transform:uppercase;color:#7d889a">Services arrêtés en ce moment</p>'
                 '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#fff5f6;border:1px solid #f5c2c4;'
                 'border-radius:14px;border-collapse:separate">%s</table></td></tr>' % (F, lignes))

    date_txt = maintenant.strftime("%d/%m/%Y")
    if arretes:
        intro = "Voici l'état des environnements au %s à %s : %s actuellement." % (
            date_txt, maintenant.strftime("%H:%M"), pa(len(arretes)))
    else:
        intro = "Voici l'état des environnements au %s à %s : tous les services supervisés sont démarrés." % (
            date_txt, maintenant.strftime("%H:%M"))
    intro += " %s sur les dernières 24 heures." % ("Aucun arrêt n'a été détecté" if not total_arrets_24h
                                                  else p(total_arrets_24h, "arrêt") + " détecté" + ("s" if total_arrets_24h > 1 else ""))
    suite = "Le détail de chaque nœud et l'historique des contrôles sont disponibles sur la page de supervision."
    demo_txt = ('&nbsp;<span style="display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:#eef1f5;'
                'font-size:11px;font-weight:600;color:#7d889a;vertical-align:1px">Démo</span>') if demo else ""
    alerte = bool(arretes)
    cid = "fond-alerte" if alerte else "fond-retabli"
    corps = gabarit_mail({
        "apercu": e(intro), "F": F, "cid": cid, "cid_mso": cid.replace("fond", "banniere"),
        "t1": "Rapport", "t2": "quotidien", "pt": "#f43f5e" if alerte else "#34d399",
        "pill": pa(len(arretes)) if alerte else "Tous les services démarrés",
        "intro": e(intro), "titre_tableau": "État des environnements", "tableau": "".join(lignes_env),
        "extra": extra, "suite": e(suite), "url": e(adresse_page), "demo": demo_txt})

    texte = ["Rapport quotidien du %s" % date_txt, "", intro, "", "Environnements :"] + texte + [""]
    if arretes:
        texte.append("Services arrêtés en ce moment :")
        texte += ["  - %s · %s (%s) · %s, depuis %s" % (c, n["name"], n["host"], s["name"], s["since"]) for c, n, s in arretes]
        texte.append("")
    texte += ["Supervision : " + adresse_page, "",
              "Message automatique envoyé par FTM - Monitoring, merci de ne pas y répondre."]
    sujet = "%s[FTM - Monitoring] Rapport du %s : %s" % (
        "[DÉMO] " if demo else "", date_txt, pa(len(arretes)) if arretes else "tout est démarré")
    return construire_message(sujet, "\n".join(texte), corps, alerte, reglages)


def envoyer_par_outlook(msg, reglages):
    # Outlook sur le poste RTE : on passe par PowerShell
    import base64
    import tempfile
    dossier = tempfile.mkdtemp(prefix="ftm_mail_")
    html = msg.get_body(("html",)).get_content()
    pieces = []
    for part in msg.walk():
        cid = (part.get("Content-ID") or "").strip("<>")
        if cid and not cid.startswith("fond"):
            nom = cid + "." + part.get_content_subtype().replace("jpeg", "jpg")
            with open(os.path.join(dossier, nom), "wb") as f:
                f.write(part.get_payload(decode=True))
            pieces.append((nom, cid))
    nom = "banniere-alerte.jpg" if "cid:banniere-alerte" in html else "banniere-retabli.jpg"
    chemin = os.path.join(os.path.dirname(FICHIER_LOGO_MAIL), nom)
    if os.path.isfile(chemin):
        with open(chemin, "rb") as src, open(os.path.join(dossier, nom), "wb") as f:
            f.write(src.read())
        pieces.append((nom, nom[:-4]))
    with open(os.path.join(dossier, "corps.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(dossier, "sujet.txt"), "w", encoding="utf-8") as f:
        f.write(str(msg["Subject"]))
    with open(os.path.join(dossier, "a.txt"), "w", encoding="utf-8") as f:
        f.write("; ".join(reglages.get("destinataires") or []))
    lignes = ['$d = "%s"' % dossier.replace('"', '`"'),
              '$o = New-Object -ComObject Outlook.Application',
              '$m = $o.CreateItem(0)',
              '$m.To = Get-Content -LiteralPath (Join-Path $d "a.txt") -Raw -Encoding UTF8',
              '$m.Subject = (Get-Content -LiteralPath (Join-Path $d "sujet.txt") -Raw -Encoding UTF8).Trim()']
    for nom, cid in pieces:
        lignes.append('$a = $m.Attachments.Add((Join-Path $d "%s"))' % nom)
        lignes.append('$a.PropertyAccessor.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x3712001F", "%s")' % cid)
        lignes.append('$a.PropertyAccessor.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x7FFE000B", $true)')
    lignes += ['$m.HTMLBody = Get-Content -LiteralPath (Join-Path $d "corps.html") -Raw -Encoding UTF8',
               '$m.Send()']
    commande = base64.b64encode("\n".join(lignes).encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", commande],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout or "erreur PowerShell").strip()[:300])
        print("[ALERTE] mail envoyé par Outlook à %s : %s" % (", ".join(reglages.get("destinataires") or []), msg["Subject"]))
        return True
    except (OSError, subprocess.SubprocessError) as err:
        print("[ALERTE] envoi par Outlook impossible (%s)" % err)
        return False
    finally:
        for n in os.listdir(dossier):
            try:
                os.remove(os.path.join(dossier, n))
            except OSError:
                pass
        try:
            os.rmdir(dossier)
        except OSError:
            pass


def envoyer_mail(msg, reglages):
    if reglages.get("methode", "").lower() == "outlook":
        if os.name != "nt":
            print("[ALERTE] methode outlook : uniquement sur Windows. Sur Mac, mettre methode;smtp dans alertes.csv.")
            return False
        return envoyer_par_outlook(msg, reglages)
    serveur, port = reglages.get("serveur_smtp", ""), int(reglages.get("port") or 25)
    securite = reglages.get("securite", "").lower()
    try:
        if securite == "ssl":
            smtp = smtplib.SMTP_SSL(serveur, port, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(serveur, port, timeout=TIMEOUT)
            if securite == "starttls":
                smtp.starttls()
        with smtp:
            if reglages.get("utilisateur"):
                smtp.login(reglages["utilisateur"], os.environ.get("FTM_SMTP_MOT_DE_PASSE", ""))
            smtp.send_message(msg)
        print("[ALERTE] mail envoyé à %s : %s" % (msg["To"], msg["Subject"]))
        return True
    except (OSError, smtplib.SMTPException) as err:
        print("[ALERTE] envoi impossible (%s)" % err)
        return False


def sauver_demo(msg, prefixe="alerte"):
    # en démo on n'envoie rien : on affiche juste le sujet dans le terminal
    print("[%s démo] %s" % (prefixe.upper(), msg["Subject"]))


def envoyer_alerte(changements, demo, adresse_page):
    if not changements:
        return
    reglages = lire_alertes()
    if not demo and not reglages:
        return
    msg = preparer_mail(changements, reglages or {}, adresse_page, demo)
    if demo:
        sauver_demo(msg, "alerte")
        return
    if reglages:
        envoyer_mail(msg, reglages)


def mail_de_test(adresse_page):
    reglages = lire_alertes()
    if not reglages:
        print("alertes.csv absent ou actif;non : copiez alertes.exemple.csv en alertes.csv et remplissez-le.")
        return
    if reglages.get("methode", "").lower() != "outlook" and reglages.get("utilisateur") and not os.environ.get("FTM_SMTP_MOT_DE_PASSE"):
        print("Le serveur demande un mot de passe : définissez d'abord la variable FTM_SMTP_MOT_DE_PASSE.")
        return
    h = datetime.now().strftime("%H:%M")
    exemple = [{"env": "DEV", "noeud": "Traitement 1", "hostname": "SRV-DEV-TRT1", "service": "Transaction Manager", "ok": False, "heure": h},
               {"env": "DEV", "noeud": "Traitement 1", "hostname": "SRV-DEV-TRT1", "service": "Monitor", "ok": False, "heure": h},
               {"env": "QUA", "noeud": "Traitement 2", "hostname": "SRV-QUA-TRT2", "service": "SSH Service (SFTP)", "ok": True, "heure": h}]
    envoyer_mail(preparer_mail(exemple, reglages, adresse_page, demo=True), reglages)


def test_tcp(ip, port):
    debut = time.perf_counter()
    try:
        with socket.create_connection((ip, int(port)), timeout=TIMEOUT):
            return True, int((time.perf_counter() - debut) * 1000)
    except (OSError, ValueError):
        return False, None


def tester(ligne, demo=False):
    if demo:  # 3 % de chance d'être arrêté
        ok = random.random() > 0.03
        return ok, random.randint(3, 45) if ok else None
    return test_tcp(ligne["ip"], ligne.get("port") or 22)


def tester_tout(lignes, demo=False):
    # une seule connexion SSH par machine pour tous ses services
    resultats = [None] * len(lignes)
    groupes = {}
    autres = []
    for i, l in enumerate(lignes):
        if l.get("commande_ssh") and not demo:
            groupes.setdefault((l.get("user_ssh", ""), l["ip"]), []).append(i)
        else:
            autres.append(i)

    def verifier_machine(cle):
        user, ip = cle
        numeros = groupes[cle]
        script = "; ".join(
            "if %s >/dev/null 2>&1; then echo R%d=OK; else echo R%d=KO; fi"
            % (lignes[i]["commande_ssh"], n, n) for n, i in enumerate(numeros))
        cible = "%s@%s" % (user, ip) if user else ip
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % TIMEOUT,
               "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
               "-o", "GSSAPIAuthentication=no",
               cible, script]
        etats, erreur = {}, ""
        for essai in range(3):
            debut = time.perf_counter()
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=DELAI_SSH,
                                   stdin=subprocess.DEVNULL)
                etats = dict(re.findall(r"R(\d+)=(OK|KO)", r.stdout))
                erreur = (r.stderr or "").strip() or "code retour %d" % r.returncode
            except subprocess.TimeoutExpired:
                etats, erreur = {}, "pas de réponse en %d s" % DELAI_SSH
            except (OSError, subprocess.SubprocessError) as err:
                etats, erreur = {}, str(err)
            if etats:
                break
            time.sleep(2)
        ms = int((time.perf_counter() - debut) * 1000)
        if not etats:
            print("[SSH] %s : connexion impossible après 3 essais (%s)" % (cible, erreur[:120] or "aucune réponse"))
            for i in numeros:
                resultats[i] = (None, None)
            return
        for n, i in enumerate(numeros):
            ok = etats.get(str(n)) == "OK"
            resultats[i] = (ok, ms if ok else None)

    def verifier_ligne(i):
        resultats[i] = tester(lignes[i], demo)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(verifier_machine, list(groupes)))
        list(pool.map(verifier_ligne, autres))
    return resultats


class Superviseur:
    def __init__(self, intervalle, demo, adresse_page="http://localhost:8080"):
        self.intervalle = intervalle
        self.demo = demo
        self.adresse_page = adresse_page
        self.verrou = threading.Lock()
        self.controle_en_cours = threading.Lock()
        self.memoire = self._charger()
        self.donnees = None

    def _charger(self):
        try:
            with open(FICHIER_ETAT, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"services": {}, "journal": {}}

    def _sauver(self):
        try:
            with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
                json.dump(self.memoire, f, ensure_ascii=False)
        except OSError:
            pass

    def controler(self):
        # si un contrôle tourne déjà, on attend sa fin
        if not self.controle_en_cours.acquire(blocking=False):
            with self.controle_en_cours:
                return self.donnees
        try:
            return self._controler()
        finally:
            self.controle_en_cours.release()

    def _controler(self):
        lignes = lire_inventaire()
        resultats = tester_tout(lignes, self.demo)

        with self.verrou:
            maintenant = datetime.now()
            heure = maintenant.strftime("%H:%M")
            jour = maintenant.strftime("%Y-%m-%d")
            envs = OrderedDict()
            changements = []
            confirmation = nb_confirmation()
            maintenance = self.memoire.setdefault("maintenance", {})
            dispo = self.memoire.setdefault("dispo", {})

            for ligne, (ok, ms) in zip(lignes, resultats):
                env = ligne["env"]
                cle = "|".join((env, ligne["hostname"], ligne["service"]))
                id_noeud = env + "-" + ligne["hostname"]
                en_maintenance = id_noeud in maintenance
                mem = self.memoire["services"].setdefault(cle, {"ok": ok, "since": heure, "history": []})
                mem.setdefault("notifie", mem.get("ok"))
                ok_reel = ok

                # journal
                if ok is not None and mem["history"] and mem["ok"] is not None and mem["ok"] != ok:
                    mem["since"] = heure
                    journal = self.memoire["journal"].setdefault(env, [])
                    journal.insert(0, {"t": heure, "jour": jour, "node": ligne["noeud"],
                                       "service": ligne["service"], "ok": ok})
                    del journal[TAILLE_JOURNAL:]
                if ok is None and mem.get("ok") is not None and mem.get("history"):
                    ok, ms = mem["ok"], None  # SSH raté : dernier état connu
                    ligne = dict(ligne, ancien=True)
                elif ok is not None:
                    mem["ok"] = ok
                    mem["history"] = (mem["history"] + [1 if ok else 0])[-TAILLE_HISTO:]

                if ok_reel is not None:
                    # alerte si confirmée et pas en maintenance
                    if mem["notifie"] is None:
                        mem["notifie"] = ok_reel
                    recents = mem["history"][-confirmation:]
                    if (mem["notifie"] != ok_reel and not en_maintenance and len(recents) == confirmation
                            and all(v == (1 if ok_reel else 0) for v in recents)):
                        mem["notifie"] = ok_reel
                        changements.append({"env": env, "noeud": ligne["noeud"], "hostname": ligne["hostname"],
                                            "service": ligne["service"], "ok": ok_reel, "heure": heure})
                    # dispo du jour
                    if not en_maintenance:
                        jours = dispo.setdefault(cle, {})
                        compteur = jours.setdefault(jour, [0, 0])
                        compteur[0] += 1 if ok_reel else 0
                        compteur[1] += 1
                        for ancien_jour in sorted(jours)[:-JOURS_DISPO]:
                            del jours[ancien_jour]
                totaux = list(zip(*dispo.get(cle, {}).values()))
                dispo7 = round(100 * sum(totaux[0]) / sum(totaux[1]), 1) if totaux and sum(totaux[1]) else None

                e = envs.setdefault(env, {"code": env, "name": NOMS_ENV.get(env, env),
                                          "cluster_csv": "", "nodes": OrderedDict()})
                if ligne.get("cluster"):
                    e["cluster_csv"] = ligne["cluster"]
                n = e["nodes"].setdefault(ligne["noeud"], {
                    "id": env + "-" + ligne["hostname"], "name": ligne["noeud"],
                    "role": ligne.get("type") or "traitement", "host": ligne["hostname"],
                    "ip": ligne["ip"], "sshUser": ligne.get("user_ssh", ""),
                    "maintenance": maintenance.get(id_noeud), "links": [], "services": []})
                if ligne.get("url"):
                    n["links"].append({"label": "Ouvrir " + ligne["service"], "url": ligne["url"]})
                n["services"].append({"name": ligne["service"], "cat": ligne.get("categorie") or "service", "ok": ok, "ms": ms,
                                      "since": mem["since"], "history": mem["history"], "dispo7": dispo7,
                                      "ancien": bool(ligne.get("ancien"))})

            liste = []
            for code, e in envs.items():
                noeuds = list(e["nodes"].values())
                if e["cluster_csv"]:
                    cluster = e["cluster_csv"].lower() in ("oui", "o", "yes", "1", "true")
                else:
                    cluster = sum(1 for n in noeuds if n["role"] == "traitement") >= 2
                journal = [j for j in self.memoire["journal"].get(code, []) if j.get("jour") == jour]
                liste.append({"code": code, "name": e["name"], "cluster": cluster,
                              "nodes": noeuds, "journal": journal})

            self.donnees = {"generatedAt": maintenant.isoformat(timespec="seconds"),
                            "source": "demo" if self.demo else ("reel" if trouver_csv() else "exemple"),
                            "interval": self.intervalle, "confirmation": confirmation,
                            "rapport": "%02d:%02d" % heure_rapport() if heure_rapport() else "",
                            "envs": liste}
            self._sauver()

        arretes = sum(1 for ok, _ in resultats if ok is False)
        print("[%s] %d services contrôlés, %d arrêté(s)" % (maintenant.strftime("%H:%M:%S"), len(resultats), arretes))
        envoyer_alerte(changements, self.demo, self.adresse_page)
        return self.donnees

    def maintenance(self, id_noeud, actif):
        with self.verrou:
            if not self.donnees:
                return None
            for env in self.donnees["envs"]:
                noeud = next((n for n in env["nodes"] if n["id"] == id_noeud), None)
                if noeud:
                    break
            else:
                return None
            maintenant = datetime.now()
            liste = self.memoire.setdefault("maintenance", {})
            if bool(actif) == (id_noeud in liste):
                return self.donnees
            if actif:
                liste[id_noeud] = {"depuis": maintenant.strftime("%H:%M"), "jour": maintenant.strftime("%Y-%m-%d")}
            else:
                liste.pop(id_noeud, None)
            noeud["maintenance"] = liste.get(id_noeud)
            journal = self.memoire["journal"].setdefault(env["code"], [])
            journal.insert(0, {"t": maintenant.strftime("%H:%M"), "jour": maintenant.strftime("%Y-%m-%d"),
                               "node": noeud["name"], "service": "Maintenance", "ok": not actif, "type": "maintenance"})
            del journal[TAILLE_JOURNAL:]
            env["journal"] = [j for j in journal if j.get("jour") == maintenant.strftime("%Y-%m-%d")]
            self._sauver()
            print("[MAINTENANCE] %s %s" % (noeud["host"], "en maintenance" if actif else ": fin de maintenance"))
            return self.donnees

    def journal_csv(self):
        lignes = [["Date", "Heure", "Environnement", "Nœud", "Service", "Événement"]]
        with self.verrou:
            evts = [(code, j) for code, liste in self.memoire.get("journal", {}).items() for j in liste]
        evts.sort(key=lambda x: (x[1].get("jour", ""), x[1].get("t", "")), reverse=True)
        for code, j in evts:
            if j.get("type") == "maintenance":
                quoi = "Fin de maintenance" if j.get("ok") else "Début de maintenance"
            else:
                quoi = "Redémarré" if j.get("ok") else "Arrêté"
            jour = j.get("jour", "")
            date = "/".join(reversed(jour.split("-"))) if jour else ""
            lignes.append([date, j.get("t", ""), code, j.get("node", ""), j.get("service", ""), quoi])
        sortie = []
        for l in lignes:
            sortie.append(";".join('"%s"' % str(c).replace('"', '""') if (";" in str(c) or '"' in str(c)) else str(c) for c in l))
        return "\ufeff" + "\r\n".join(sortie) + "\r\n"

    def rapport(self, force=False):
        # une fois par jour, à l'heure de rapport_quotidien
        h = heure_rapport()
        maintenant = datetime.now()
        jour = maintenant.strftime("%Y-%m-%d")
        if not force:
            if not h or (maintenant.hour, maintenant.minute) < h or self.memoire.get("rapport_envoye") == jour:
                return
        reglages = lire_alertes()
        if not self.demo and not reglages:
            if force:
                print("alertes.csv absent ou actif;non : le rapport ne peut pas être envoyé.")
            return
        if not self.donnees:
            return
        with self.verrou:
            msg = preparer_rapport(self.donnees, self.memoire.get("journal", {}), reglages or {},
                                   self.adresse_page, self.demo)
        if self.demo:
            sauver_demo(msg, "rapport")
        elif not envoyer_mail(msg, reglages):
            return
        with self.verrou:
            self.memoire["rapport_envoye"] = jour
            self._sauver()

    def boucle(self):
        while True:
            time.sleep(self.intervalle)
            try:
                self.controler()
                self.rapport()
            except Exception as err:
                print("Erreur pendant le contrôle :", err)


def fabriquer_handler(sup):
    class Handler(BaseHTTPRequestHandler):
        def _envoyer(self, code, contenu, type_):
            corps = contenu.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", type_ + "; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                chemin = trouver_page()
                if not chemin:
                    self._envoyer(500, "index.html introuvable dans templates/", "text/plain")
                    return
                try:
                    with open(chemin, encoding="utf-8-sig") as f:
                        page = f.read()
                except (OSError, UnicodeDecodeError) as err:
                    self._envoyer(500, "Impossible de lire " + chemin + " : " + str(err), "text/plain")
                    return
                with sup.verrou:
                    donnees = json.dumps(sup.donnees, ensure_ascii=False).replace("</", "<\\/")
                page = page.replace("/*__DATA__*/null", donnees)
                self._envoyer(200, page, "text/html")
            elif self.path.startswith("/police/"):
                nom = os.path.basename(self.path)
                chemin = os.path.join(DOSSIER, "templates", "police", nom)
                if nom.endswith(".woff") and os.path.isfile(chemin):
                    with open(chemin, "rb") as f:
                        corps = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "font/woff")
                    self.send_header("Content-Length", str(len(corps)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(corps)
                else:
                    self._envoyer(404, "Police introuvable", "text/plain")
            elif self.path == "/api/etat":
                with sup.verrou:
                    self._envoyer(200, json.dumps(sup.donnees, ensure_ascii=False), "application/json")
            elif self.path.split("?")[0] == "/api/journal.csv":
                corps = sup.journal_csv().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="journal-ftm-%s.csv"'
                                 % datetime.now().strftime("%Y-%m-%d"))
                self.send_header("Content-Length", str(len(corps)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(corps)
            else:
                self._envoyer(404, "Page introuvable", "text/plain")

        def do_POST(self):
            if self.path == "/api/controle":
                self._envoyer(200, json.dumps(sup.controler(), ensure_ascii=False), "application/json")
            elif self.path == "/api/maintenance":
                try:
                    taille = min(int(self.headers.get("Content-Length") or 0), 10000)
                    demande = json.loads(self.rfile.read(taille) or b"{}")
                    donnees = sup.maintenance(str(demande.get("id", "")), bool(demande.get("actif")))
                except (ValueError, AttributeError):
                    donnees = None
                if donnees is None:
                    self._envoyer(404, json.dumps({"erreur": "nœud inconnu"}), "application/json")
                else:
                    with sup.verrou:
                        self._envoyer(200, json.dumps(donnees, ensure_ascii=False), "application/json")
            else:
                self._envoyer(404, "Page introuvable", "text/plain")

        def log_message(self, *args):
            pass
    return Handler


def main():
    p = argparse.ArgumentParser(description="Supervision des environnements FTM")
    p.add_argument("--port", type=int, default=8080, help="port du serveur web (8080)")
    p.add_argument("--intervalle", type=int, default=60, help="secondes entre deux contrôles (60)")
    p.add_argument("--demo", action="store_true", help="états simulés, sans tester le réseau")
    p.add_argument("--test-mail", action="store_true", help="envoie une alerte d'exemple puis s'arrête")
    p.add_argument("--test-rapport", action="store_true", help="fait un contrôle, envoie le rapport quotidien puis s'arrête")
    a = p.parse_args()
    global DEMO
    DEMO = a.demo
    adresse_page = "http://%s:%d" % (socket.gethostname(), a.port)
    if a.test_mail:
        mail_de_test(adresse_page)
        return
    if a.test_rapport:
        sup = Superviseur(a.intervalle, a.demo, adresse_page)
        sup.controler()
        sup.rapport(force=True)
        return

    csv_reel = trouver_csv()
    print("Dossier du script :", DOSSIER)
    print("Page web :", trouver_page() or "INTROUVABLE (mettre index.html dans le dossier templates)")
    if a.demo:
        print("MODE DÉMO : machines fictives, aucun mail envoyé (pour tester : --test-mail)")
    elif csv_reel:
        print("Machines lues dans :", csv_reel)
    else:
        print("ATTENTION : environnements.csv introuvable -> machines fictives")
    if not a.demo:
        print("Alertes mail :", "activées" if lire_alertes() else "désactivées (voir alertes.csv)")
    print("Confirmation : alerte après %d contrôle(s) identique(s)" % nb_confirmation())
    if heure_rapport():
        print("Rapport quotidien : chaque jour à %02d:%02d" % heure_rapport())
    sup = Superviseur(a.intervalle, a.demo, adresse_page)
    sup.controler()
    threading.Thread(target=sup.boucle, daemon=True).start()

    serveur = ThreadingHTTPServer(("0.0.0.0", a.port), fabriquer_handler(sup))
    print("Supervision FTM disponible sur http://localhost:%d%s" % (a.port, "  (mode démo)" if a.demo else ""))
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")


if __name__ == "__main__":
    main()