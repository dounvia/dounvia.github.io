"""Tests automatiques de FTM - Monitoring.

Lancement, depuis le dossier du projet :
    python -m unittest discover -s tests -v

Les tests n'utilisent que des machines fictives : aucune connexion SSH,
aucun mail envoyé, aucun fichier du projet modifié (l'état est écrit
dans un dossier temporaire).
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import supervision as sup  # noqa: E402


def services_csv():
    return sup.lire_csv(sup.FICHIER_SERVICES)


class TestInventaire(unittest.TestCase):
    def setUp(self):
        self.ancien_demo = sup.DEMO
        sup.DEMO = True          # inventaire fictif uniquement

    def tearDown(self):
        sup.DEMO = self.ancien_demo

    def test_inventaire_fictif(self):
        lignes = sup.lire_inventaire()
        noeuds = {(l["env"], l["hostname"]) for l in lignes}
        self.assertEqual(len(noeuds), 8, "8 nœuds attendus dans l'inventaire d'exemple")
        par_noeud = len(lignes) // len(noeuds)
        self.assertEqual(par_noeud, len(services_csv()), "chaque nœud doit avoir tous les services")

    def test_demo_ne_lit_jamais_le_vrai_inventaire(self):
        for l in sup.lire_inventaire():
            self.assertTrue(l["hostname"].startswith("SRV-"), "en démo, seules les machines fictives sont lues")

    def test_environnements_connus(self):
        envs = {l["env"] for l in sup.lire_inventaire()}
        self.assertEqual(envs, {"QUA", "DEV", "INT"})


class TestMotifsPgrep(unittest.TestCase):
    """Les motifs de services.csv doivent trouver le vrai service, et rien d'autre."""

    def motifs(self):
        res = {}
        for s in services_csv():
            m = re.search(r"pgrep -f '([^']+)'", s.get("commande_ssh", ""))
            if m:
                res[s["service"]] = m.group(1)
        return res

    def test_cinq_services(self):
        self.assertEqual(len(self.motifs()), 5)

    def test_trouve_le_service(self):
        for service, motif in self.motifs().items():
            nom = motif.replace("[D]", "D").replace("( |$)", "")
            ligne = "/opt/axway/ST/jre/bin/java -Xms512M -%s -server" % nom
            self.assertRegex(ligne, motif, service)

    def test_ne_se_trouve_pas_lui_meme(self):
        # La commande de recherche contient le motif en clair : elle ne doit pas correspondre.
        for service, motif in self.motifs().items():
            commande = "bash -c if pgrep -f '%s' >/dev/null 2>&1; then echo R0=OK; fi" % motif
            self.assertIsNone(re.search(motif, commande), service)

    def test_pas_de_confusion_entre_noms_proches(self):
        motif_tm = self.motifs()["Transaction Manager"]
        self.assertIsNone(re.search(motif_tm, "java -Dct=tmx -server"))
        self.assertIsNone(re.search(motif_tm, "bash /opt/axway/ST/bin/start_tm_console"))


class TestSuperviseur(unittest.TestCase):
    """Alertes, maintenance et disponibilité, avec des résultats de contrôle simulés."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(sup, "FICHIER_ETAT", os.path.join(self.tmp.name, "etat.json")),
            mock.patch.object(sup, "DEMO", True),
            mock.patch.object(sup, "nb_confirmation", return_value=2),
            mock.patch.object(sup, "heure_rapport", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.alertes = []
        self.patches.append(mock.patch.object(sup, "envoyer_alerte",
                                              side_effect=lambda ch, demo, adr: self.alertes.append(list(ch))))
        self.patches[-1].start()
        self.lignes = sup.lire_inventaire()
        self.s = sup.Superviseur(60, demo=True)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def controle(self, arretes=()):
        """Un contrôle où les lignes d'indice `arretes` sont arrêtées, les autres démarrées."""
        res = [(False, None) if i in arretes else (True, 20) for i in range(len(self.lignes))]
        with mock.patch.object(sup, "tester_tout", return_value=res):
            return self.s.controler()

    def test_pas_d_alerte_au_premier_controle(self):
        self.controle()
        self.assertEqual(self.alertes[-1], [])

    def test_anti_fausse_alerte(self):
        self.controle()
        self.controle()
        self.controle(arretes={2})          # 1er contrôle en échec : pas encore d'alerte
        self.assertEqual(self.alertes[-1], [])
        self.controle(arretes={2})          # 2e contrôle en échec : alerte confirmée
        self.assertEqual(len(self.alertes[-1]), 1)
        self.assertFalse(self.alertes[-1][0]["ok"])
        self.controle(arretes={2})          # toujours arrêté : pas de 2e mail
        self.assertEqual(self.alertes[-1], [])

    def test_coupure_isolee_ignoree(self):
        self.controle()
        self.controle()
        self.controle(arretes={2})          # un seul contrôle en échec...
        self.controle()                     # ...puis tout va bien : aucun mail
        self.assertTrue(all(a == [] for a in self.alertes))

    def test_journal_enregistre_les_changements(self):
        self.controle()
        self.controle(arretes={2})
        env = self.lignes[2]["env"]
        journal = self.s.memoire["journal"][env]
        self.assertEqual(journal[0]["service"], self.lignes[2]["service"])
        self.assertFalse(journal[0]["ok"])

    def test_maintenance_bloque_les_alertes(self):
        self.controle()
        l = self.lignes[2]
        self.assertIsNotNone(self.s.maintenance(l["env"] + "-" + l["hostname"], True))
        self.controle(arretes={2})
        self.controle(arretes={2})
        self.assertTrue(all(a == [] for a in self.alertes), "aucune alerte pendant une maintenance")

    def test_disponibilite(self):
        self.controle()
        self.controle(arretes={2})
        self.controle(arretes={2})
        self.controle()
        l = self.lignes[2]
        cle = "|".join((l["env"], l["hostname"], l["service"]))
        ok, total = list(self.s.memoire["dispo"][cle].values())[0]
        self.assertEqual((ok, total), (2, 4))

    def test_noeud_injoignable_non_verifie(self):
        self.controle()
        res = [(None, None)] * len(self.lignes)       # aucune réponse SSH
        with mock.patch.object(sup, "tester_tout", return_value=res):
            donnees = self.s.controler()
        self.assertEqual(self.alertes[-1], [], "une connexion impossible ne doit pas déclencher d'alerte")
        service = donnees["envs"][0]["nodes"][0]["services"][0]
        self.assertTrue(service["ancien"], "le dernier état connu est affiché, marqué comme ancien")

    def test_export_csv(self):
        self.controle()
        self.controle(arretes={2})
        csv_texte = self.s.journal_csv()
        self.assertTrue(csv_texte.startswith("﻿Date;Heure;Environnement"))
        self.assertIn("Arrêté", csv_texte)


class TestMail(unittest.TestCase):
    def test_contenu_du_mail(self):
        ch = [{"env": "DEV", "noeud": "Traitement 1", "hostname": "SRV-DEV-TRT1",
               "service": "Monitor", "ok": False, "heure": "09:30"}]
        html = sup.html_mail(ch, "http://localhost:8080", True)
        for attendu in ("Bonjour", "Monitor", "SRV-DEV-TRT1", "09:30", "http://localhost:8080"):
            self.assertIn(attendu, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
