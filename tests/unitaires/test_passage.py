"""Le PASSAGE d'une livraison dans les tables : traiter un fichier avec le pipeline, puis (seulement si on le demande) l'écrire.

Deux niveaux. `passer` est la logique, qui travaille sur un MAGASIN quelconque : les tests l'essaient avec le magasin en mémoire, qui garde
l'état d'un appel à l'autre, donc on prouve pour de vrai le rejeu, la reprise et l'identité avec la journée jouée par `jouer`. Le programme
(`principal`) enveloppe cette logique avec le magasin SQL d'un exécuteur ; ses tests utilisent un faux exécuteur qui note chaque requête,
pour prouver ce qui part, dans quel ordre, et qu'en simulation (par défaut) il ne part que des lectures.

Rien ne tourne sur un vrai workspace : seul le premier lancement réel dira ce que rend le service.

Le module est importé par son nom (`passage.passer`) pour qu'un module encore vide fasse échouer chaque test sur son assertion, et non la
collecte de tout le fichier.
"""
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rd_manuf import passage
from rd_manuf.alertes import charger_catalogue
from rd_manuf.ecriture import EtatMagasin, MagasinEnMemoire, couches
from rd_manuf.journee import Etat, jouer, journee_multi_usines

RACINE = Path(__file__).resolve().parents[2]
CHEMIN_CONTRAT = RACINE / "contracts" / "data" / "livraison_production.yaml"
CHEMIN_ALERTES = RACINE / "contracts" / "alertes" / "catalogue.yaml"
PLAYGROUND = "rd_manuf_playground"
MAINTENANT = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CLES_DE_LIGNE = ["--contrat", str(CHEMIN_CONTRAT), "--alertes", str(CHEMIN_ALERTES)]


# ==============================================================================
# 🧰 LES OUTILS : un faux exécuteur qui note tout, des fichiers de la journée de test
# ==============================================================================
class FauxExecuteur:
    """Rend toujours « aucune ligne » (tables vides) et note chaque requête ; `pannes` : (morceau de requête, exception à lever)."""

    def __init__(self, pannes=()):
        self.envoyes = []
        self.pannes = list(pannes)

    def __call__(self, sql, parametres):
        self.envoyes.append((sql, dict(parametres)))
        for morceau, erreur in self.pannes:
            if morceau in sql:
                raise erreur
        return []

    @property
    def textes(self):
        return [sql for sql, _ in self.envoyes]


TABLES_DU_PLAYGROUND = [
    ("ops", "registre"), ("bronze", "lignes"), ("bronze", "lignes_coupees"), ("silver", "lignes"), ("silver", "quarantaine"),
]


class ExecuteurAvecTables(FauxExecuteur):
    """Comme le faux exécuteur, mais le catalogue d'information (`information_schema`) dit quelles tables existent."""

    def __init__(self, tables=TABLES_DU_PLAYGROUND, pannes=()):
        super().__init__(pannes)
        self.tables = list(tables)

    def __call__(self, sql, parametres):
        rendu = super().__call__(sql, parametres)
        if "information_schema.schemata" in sql:
            return [{"schema_name": schema} for schema in sorted({schema for schema, _ in self.tables})]
        if "information_schema.tables" in sql:
            return [{"table_schema": schema, "table_name": nom} for schema, nom in self.tables]
        return rendu


LECTURES_DE_TABLES = "SELECT * FROM `rd_manuf_playground`.`"


def _est_une_lecture(sql):
    return sql.lstrip().upper().startswith("SELECT")


@pytest.fixture
def sortie():
    return []


@pytest.fixture(scope="module")
def livraisons(contrat):
    return journee_multi_usines(contrat)


def _fichier(contrat, livraisons, etiquette, dossier):
    livraison = next(livraison for livraison in livraisons if livraison.etiquette == etiquette)
    chemin = Path(dossier) / livraison.nom
    livraison.ecrire(contrat, chemin)
    return chemin, livraison


def _lancer(chemin, executeur, sortie, *options, **branchements):
    argv = ["--profil", "mon-profil", "--entrepot", "abc123", "--fichier", str(chemin), *CLES_DE_LIGNE, *options]
    fabrique = branchements.pop("fabriquer", lambda profil, entrepot, attente_max=None: executeur)
    branchements.setdefault("maintenant", lambda: MAINTENANT)
    return passage.principal(argv, fabriquer=fabrique, sortie=sortie.append, **branchements)


def _texte(sortie):
    return "\n".join(sortie)


# ==============================================================================
# 1. 🧠 LA LOGIQUE : `passer` sur un magasin en mémoire
# ==============================================================================
def _etat(contrat, magasin):
    return EtatMagasin(contrat, magasin)


def _alertes(contrat):
    return charger_catalogue(CHEMIN_ALERTES)


def test_une_livraison_saine_ecrite_remplit_toutes_les_couches_et_le_registre_en_dernier(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()

    resultat = passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)

    etat = _etat(contrat, magasin)
    assert resultat.ecrit is True and resultat.heure_arrivee == livraison.arrivee
    assert [ligne.statut for ligne in etat.registre] == ["ACCEPTEE"]
    assert (len(etat.lignes_bronze), len(etat.lignes_silver), len(etat.quarantaine)) == (100, 100, 0)


def test_sans_ecrire_rien_n_est_ecrit_mais_le_resultat_est_rendu(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)
    magasin = MagasinEnMemoire()

    resultat = passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=False)

    etat = _etat(contrat, magasin)
    assert resultat.ecrit is False
    assert (etat.registre, etat.lignes_bronze, etat.lignes_silver, etat.quarantaine) == ([], [], [], [])
    assert resultat.resultat.ligne_registre.statut == "ACCEPTEE_AVEC_REJETS" and len(resultat.resultat.silver.quarantaine) == 2


def test_une_livraison_bloquee_garde_son_bronze_et_n_a_rien_au_silver(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "usa-ca-001-bloquee", tmp_path)
    magasin = MagasinEnMemoire()

    passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)

    etat = _etat(contrat, magasin)
    assert [ligne.statut for ligne in etat.registre] == ["BLOQUEE"]
    assert (len(etat.lignes_bronze), len(etat.lignes_silver)) == (100, 0)


def test_une_livraison_refusee_n_ecrit_que_son_registre(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "fra-aa-004-colonne-manquante", tmp_path)
    magasin = MagasinEnMemoire()

    passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)

    etat = _etat(contrat, magasin)
    assert [ligne.statut for ligne in etat.registre] == ["REFUSEE"]
    assert (etat.lignes_bronze, etat.lignes_silver, etat.quarantaine) == ([], [], [])


def test_la_meme_livraison_repassee_plus_tard_est_un_rejeu_qui_n_ajoute_aucune_ligne_de_donnees(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()
    passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)

    rejeu = passage.passer(contrat, _alertes(contrat), chemin, magasin, MAINTENANT, ecrire=True)

    etat = _etat(contrat, magasin)
    assert rejeu.resultat.ligne_registre.statut == "REJEU_IGNORE"
    assert sorted(ligne.statut for ligne in etat.registre) == ["ACCEPTEE", "REJEU_IGNORE"]
    assert (len(etat.lignes_bronze), len(etat.lignes_silver)) == (100, 100)


def test_repasser_avec_la_meme_heure_d_arrivee_ne_duplique_rien(contrat, livraisons, tmp_path):
    """C'est la reprise : une livraison interrompue se relance avec l'heure qui était affichée, et ne change rien de ce qui est écrit."""
    chemin, livraison = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)
    magasin = MagasinEnMemoire()
    passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)
    avant = _etat(contrat, magasin)
    comptes = (len(avant.registre), len(avant.lignes_bronze), len(avant.lignes_silver), len(avant.quarantaine))

    passage.passer(contrat, _alertes(contrat), chemin, magasin, livraison.arrivee, ecrire=True)

    apres = _etat(contrat, magasin)
    assert (len(apres.registre), len(apres.lignes_bronze), len(apres.lignes_silver), len(apres.quarantaine)) == comptes
    assert [ligne.statut for ligne in apres.registre] == ["ACCEPTEE_AVEC_REJETS"]


def test_une_heure_d_arrivee_sans_fuseau_est_refusee_par_la_logique(contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    with pytest.raises(ValueError, match="fuseau"):
        passage.passer(contrat, _alertes(contrat), chemin, MagasinEnMemoire(), datetime(2026, 11, 14, 12, 0), ecrire=True)


def test_un_fichier_introuvable_au_nom_valide_n_est_pas_rattrape_par_la_logique(contrat, tmp_path):
    """Au nom invalide, le pipeline refuse la livraison sans lire le fichier : seul un nom conforme mais absent lève l'erreur de fichier."""
    absent = tmp_path / "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"

    with pytest.raises(FileNotFoundError):
        passage.passer(contrat, _alertes(contrat), absent, MagasinEnMemoire(), MAINTENANT, ecrire=True)


def test_passer_la_journee_fichier_par_fichier_donne_l_etat_de_la_journee_jouee_par_jouer(contrat, livraisons, tmp_path):
    catalogue = _alertes(contrat)
    attendu = Etat()
    jouer(contrat, catalogue, livraisons, tmp_path / "jouee", attendu)
    magasin = MagasinEnMemoire()
    dossier = tmp_path / "passee"
    dossier.mkdir()

    for livraison in livraisons:
        chemin = dossier / livraison.nom
        livraison.ecrire(contrat, chemin)
        passage.passer(contrat, catalogue, chemin, magasin, livraison.arrivee, ecrire=True)

    etat = _etat(contrat, magasin)
    assert len(attendu.registre) == 17 and len(attendu.lignes_silver) > 600                 # une vraie journée, pas un test à vide
    assert etat.registre == attendu.registre
    assert etat.lignes_bronze == attendu.lignes_bronze and etat.lignes_coupees == attendu.lignes_coupees
    assert etat.lignes_silver == attendu.lignes_silver and etat.quarantaine == attendu.quarantaine


# ==============================================================================
# 2. 🚪 LE LANCEMENT : arguments, gardes, tout ce qui se refuse AVANT de se connecter
# ==============================================================================
class ConnexionInterdite(BaseException):
    """Hérite de BaseException, pas d'Exception : le lanceur rattrape toute `Exception` de connexion et en ferait « Connexion impossible »,
    code 2, ce qui ferait passer pour réussi un test qui exige de ne PAS se connecter."""


def _jamais_appele(*_args, **_kwargs):
    raise ConnexionInterdite("la fabrique ne doit pas être appelée : on ne se connecte pas")


@pytest.mark.parametrize("manquant", ["--profil", "--entrepot", "--fichier"])
def test_profil_entrepot_et_fichier_sont_obligatoires(manquant, sortie, tmp_path):
    argv = ["--profil", "mon-profil", "--entrepot", "abc123", "--fichier", str(tmp_path / "x.csv"), *CLES_DE_LIGNE]
    position = argv.index(manquant)
    del argv[position:position + 2]

    with pytest.raises(SystemExit) as sortie_argparse:
        passage.principal(argv, fabriquer=_jamais_appele, sortie=sortie.append)

    assert sortie_argparse.value.code == 2


@pytest.mark.parametrize("catalogue", ["rd_manuf_dev", "rd_manuf_prod", "space_1"])
def test_un_catalogue_autre_que_le_playground_est_refuse_avant_toute_connexion(catalogue, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    code = _lancer(chemin, None, sortie, "--catalogue", catalogue, fabriquer=_jamais_appele)

    assert code == 2
    assert PLAYGROUND in _texte(sortie) and catalogue in _texte(sortie)


def test_le_catalogue_par_defaut_est_le_playground(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")

    assert any(f"`{PLAYGROUND}`" in sql for sql in executeur.textes)


def test_un_fichier_introuvable_est_refuse_avant_toute_connexion_en_disant_lequel(sortie, tmp_path):
    code = _lancer(tmp_path / "absent.csv", None, sortie, fabriquer=_jamais_appele)

    assert code == 2 and "absent.csv" in _texte(sortie) and "introuvable" in _texte(sortie).lower()


def test_un_dossier_au_lieu_d_un_fichier_est_refuse_avant_toute_connexion(sortie, tmp_path):
    assert _lancer(tmp_path, None, sortie, fabriquer=_jamais_appele) == 2
    assert "Fichier introuvable" in _texte(sortie)


def test_un_contrat_illisible_est_refuse_avant_toute_connexion_avec_la_marche_a_suivre(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    argv = ["--profil", "p", "--entrepot", "e", "--fichier", str(chemin)]
    argv += ["--contrat", str(tmp_path / "absent.yaml"), "--alertes", str(CHEMIN_ALERTES)]

    code = passage.principal(argv, fabriquer=_jamais_appele, sortie=sortie.append)

    assert code == 2 and "--contrat" in _texte(sortie)


def test_un_catalogue_d_alertes_illisible_est_refuse_avant_toute_connexion_avec_la_marche_a_suivre(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    argv = ["--profil", "p", "--entrepot", "e", "--fichier", str(chemin)]
    argv += ["--contrat", str(CHEMIN_CONTRAT), "--alertes", str(tmp_path / "absent.yaml")]

    code = passage.principal(argv, fabriquer=_jamais_appele, sortie=sortie.append)

    assert code == 2 and "--alertes" in _texte(sortie)


def test_les_chemins_par_defaut_sont_ceux_du_depot(monkeypatch, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    monkeypatch.chdir(RACINE)
    argv = ["--profil", "p", "--entrepot", "e", "--fichier", str(chemin)]

    fabriquer = lambda p, e, attente_max=None: FauxExecuteur()  # noqa: E731

    assert passage.principal(argv, fabriquer=fabriquer, sortie=sortie.append, maintenant=lambda: MAINTENANT) == 0


HEURES_INVALIDES = [
    ("2026-11-12T10:00:00", "fuseau"), ("demain", "ISO 8601"), ("", "ISO 8601"), ("2026-13-45T00:00:00Z", "ISO 8601"),
]


@pytest.mark.parametrize(("valeur", "raison"), HEURES_INVALIDES, ids=["sans fuseau", "texte", "vide", "date impossible"])
def test_une_heure_invalide_est_refusee_avant_toute_connexion_en_disant_pourquoi(valeur, raison, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    code = _lancer(chemin, None, sortie, "--heure-arrivee", valeur, fabriquer=_jamais_appele)

    assert code == 2 and "--heure-arrivee" in _texte(sortie) and raison in _texte(sortie)


@pytest.mark.parametrize(("valeur", "attendu"), [
    ("2026-11-12T09:00:00Z", "2026-11-12 09:00:00 UTC"),
    ("2026-11-12T10:00:00+01:00", "2026-11-12 09:00:00 UTC"),
    ("2026-11-12 09:00:00+00:00", "2026-11-12 09:00:00 UTC"),
])
def test_une_heure_d_arrivee_avec_fuseau_est_acceptee_et_affichee_en_utc(valeur, attendu, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    assert _lancer(chemin, FauxExecuteur(), sortie, "--heure-arrivee", valeur) == 0
    assert f"Arrivée : {attendu}" in _texte(sortie)


def test_sans_heure_d_arrivee_c_est_maintenant_en_utc(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie)

    assert "Arrivée : 2026-11-14 12:00:00 UTC" in _texte(sortie)


def test_le_delai_est_transmis_a_la_fabrique_et_vaut_120_secondes_par_defaut(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    recus = []

    def fabriquer(profil, entrepot, attente_max=None):
        recus.append((profil, entrepot, attente_max))
        return FauxExecuteur()

    _lancer(chemin, None, sortie, fabriquer=fabriquer)
    _lancer(chemin, None, sortie, "--delai", "45", fabriquer=fabriquer)

    assert recus == [("mon-profil", "abc123", 120.0), ("mon-profil", "abc123", 45.0)]


@pytest.mark.parametrize("valeur", ["0", "-1", "nan", "inf", "abc"])
def test_un_delai_invalide_est_refuse_par_l_analyseur(valeur, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    with pytest.raises(SystemExit):
        _lancer(chemin, None, sortie, "--delai", valeur, fabriquer=_jamais_appele)


# ==============================================================================
# 3. 🔍 LA SIMULATION : par défaut, seulement des lectures
# ==============================================================================
def test_par_defaut_il_ne_part_que_des_lectures(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)
    executeur = FauxExecuteur()

    code = _lancer(chemin, executeur, sortie)

    assert code == 0 and executeur.textes
    assert all(_est_une_lecture(sql) for sql in executeur.textes), [sql for sql in executeur.textes if not _est_une_lecture(sql)]
    assert not any(re.search(r"\b(CREATE|MERGE|INSERT|DROP|DELETE|UPDATE|ALTER)\b", sql, re.IGNORECASE) for sql in executeur.textes)


def test_la_premiere_requete_est_la_sonde_de_connexion(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie)

    assert executeur.textes[0] == passage.SQL_CONNEXION


# ------------------------------------------------------------------------------
# La simulation sur un playground où les tables manquent : la décision est rendue, avec un état supposé vide
# ------------------------------------------------------------------------------
def test_en_simulation_sur_un_playground_vierge_la_decision_est_rendue_avec_un_etat_suppose_vide(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)
    executeur = ExecuteurAvecTables(tables=[], pannes=[("FROM `rd_manuf_playground`.`", RuntimeError("[TABLE_OR_VIEW_NOT_FOUND]"))])

    code = _lancer(chemin, executeur, sortie)
    texte = _texte(sortie)

    assert code == 0 and "Statut : ACCEPTEE_AVEC_REJETS" in texte
    assert "Tables absentes ou invisibles pour ce profil" in texte and "supposé vide" in texte
    assert "Simulation : rien n'a été créé ni écrit" in texte
    assert not any(sql.startswith(LECTURES_DE_TABLES) for sql in executeur.textes)          # on ne lit pas une table qui n'existe pas


def test_avec_une_partie_des_tables_la_simulation_dit_lesquelles_manquent_et_part_d_un_etat_vide(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = ExecuteurAvecTables(tables=[("ops", "registre")])

    code = _lancer(chemin, executeur, sortie)
    ligne = next(ligne for ligne in sortie if ligne.startswith("Tables absentes"))

    assert code == 0
    assert "silver.lignes" in ligne and "silver.quarantaine" in ligne and "bronze.lignes_coupees" in ligne and "ops.registre" not in ligne
    assert not any(sql.startswith(LECTURES_DE_TABLES) for sql in executeur.textes)


def test_quand_les_cinq_tables_existent_la_simulation_les_lit_et_ne_dit_rien_d_absent(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = ExecuteurAvecTables()

    code = _lancer(chemin, executeur, sortie)

    assert code == 0 and "Tables absentes" not in _texte(sortie)
    assert any(sql.startswith(LECTURES_DE_TABLES + "ops`.`registre`") for sql in executeur.textes)
    assert all(_est_une_lecture(sql) for sql in executeur.textes)


def test_une_lecture_qui_echoue_alors_que_les_tables_existent_reste_un_echec(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = ExecuteurAvecTables(pannes=[(LECTURES_DE_TABLES + "ops`.`registre`", RuntimeError("droit manquant"))])

    code = _lancer(chemin, executeur, sortie)

    assert code == 1 and "Lecture de l'état impossible" in _texte(sortie) and "droit manquant" in _texte(sortie)


def test_une_verification_de_presence_qui_echoue_est_un_echec_de_lecture(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = ExecuteurAvecTables(pannes=[("information_schema", RuntimeError("catalogue illisible"))])

    code = _lancer(chemin, executeur, sortie)

    assert code == 1 and "Lecture de l'état impossible" in _texte(sortie) and "catalogue illisible" in _texte(sortie)


def test_avec_le_drapeau_les_tables_sont_creees_donc_la_presence_n_est_pas_verifiee(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = ExecuteurAvecTables(tables=[])

    _lancer(chemin, executeur, sortie, "--ecrire")

    assert not any("information_schema" in sql for sql in executeur.textes) and "Tables absentes" not in _texte(sortie)


# ------------------------------------------------------------------------------
# L'heure affichée est l'heure qui sert de clé
# ------------------------------------------------------------------------------
def test_l_heure_par_defaut_est_tronquee_a_la_seconde_pour_que_l_affichage_soit_la_cle(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("MERGE INTO `rd_manuf_playground`.`silver`.`lignes`", ConnectionError("réseau"))])

    _lancer(chemin, executeur, sortie, "--ecrire", maintenant=lambda: datetime(2026, 11, 14, 12, 0, 0, 654321, tzinfo=UTC))
    texte = _texte(sortie)

    assert "Arrivée : 2026-11-14 12:00:00 UTC" in texte
    assert "--heure-arrivee 2026-11-14T12:00:00Z" in texte                  # la reprise reprend exactement ce qui est affiché


def test_une_heure_donnee_avec_des_microsecondes_est_affichee_avec_elles(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie, "--heure-arrivee", "2026-11-12T09:00:00.500000Z")

    assert "Arrivée : 2026-11-12 09:00:00.500000 UTC" in _texte(sortie)


# ------------------------------------------------------------------------------
# Une écriture refusée parce qu'une livraison plus ancienne est à reprendre : le bon conseil, le message entier
# ------------------------------------------------------------------------------
def test_une_ecriture_refusee_pour_une_livraison_a_reprendre_dit_de_la_reprendre_d_abord_avec_le_message_entier(
    monkeypatch, sortie, contrat, livraisons, tmp_path
):
    ancienne = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    a_passer = _fichier(contrat, livraisons, "fra-aa-002-parquet-saine", tmp_path)
    magasin = MagasinEnMemoire()
    alertes = charger_catalogue(CHEMIN_ALERTES)
    resultat = passage.passer(contrat, alertes, ancienne[0], MagasinEnMemoire(), ancienne[1].arrivee, ecrire=False).resultat
    bronze = couches(contrat)["bronze"]
    enregistrements = [bronze.vers_enregistrement(ligne) for ligne in resultat.lignes_bronze]
    magasin.inserer_absentes(bronze.table, bronze.cles, enregistrements)                       # les lignes, sans leur registre
    monkeypatch.setattr(passage, "MagasinSql", lambda executeur, catalogue: magasin)
    arrivee = a_passer[1].arrivee.strftime("%Y-%m-%dT%H:%M:%SZ")

    code = _lancer(a_passer[0], FauxExecuteur(), sortie, "--ecrire", "--heure-arrivee", arrivee)
    texte = _texte(sortie)

    assert code == 1 and "Écriture refusée, rien n'a été écrit" in texte
    assert ancienne[1].nom in texte and "arrivée 2026-11-12 09:00:00 UTC" in texte               # le message entier : de quoi agir
    assert "n'ont pas de ligne de registre" in texte                                            # jusqu'au bout : pas borné à 200 caractères
    assert "Reprendre d'abord la livraison citée" in texte
    assert "Des couches ont pu être écrites" not in texte and "sans rien dupliquer" not in texte


# ------------------------------------------------------------------------------
# Ctrl+C : un message par phase
# ------------------------------------------------------------------------------
def test_une_interruption_pendant_la_connexion_avec_le_drapeau_dit_que_rien_n_a_ete_cree(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    def interrompue(profil, entrepot, attente_max=None):
        raise KeyboardInterrupt

    code = _lancer(chemin, None, sortie, "--ecrire", fabriquer=interrompue)

    assert code == 130 and "Rien n'a été créé ni écrit" in _texte(sortie) and "ont pu être créées" not in _texte(sortie)


def test_une_interruption_pendant_la_lecture_avec_le_drapeau_dit_que_les_tables_existent_et_qu_aucune_ligne_n_est_ecrite(
    sortie, contrat, livraisons, tmp_path
):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    code = _lancer(chemin, FauxExecuteur(pannes=[("SELECT * FROM", KeyboardInterrupt())]), sortie, "--ecrire")

    assert code == 130 and "les tables existent" in _texte(sortie) and "aucune ligne de la livraison n'a été écrite" in _texte(sortie)


def test_une_interruption_pendant_le_traitement_avec_le_drapeau_dit_que_les_tables_existent(
    monkeypatch, sortie, contrat, livraisons, tmp_path
):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    def interrompu(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(passage, "traiter_livraison", interrompu)
    code = _lancer(chemin, FauxExecuteur(), sortie, "--ecrire")

    assert code == 130 and "les tables existent" in _texte(sortie) and "aucune ligne de la livraison n'a été écrite" in _texte(sortie)


def test_une_interruption_pendant_la_creation_dit_que_des_tables_ont_pu_etre_creees(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    code = _lancer(chemin, FauxExecuteur(pannes=[("CREATE SCHEMA", KeyboardInterrupt())]), sortie, "--ecrire")

    assert code == 130 and "des tables ont pu être créées" in _texte(sortie) and "aucune de ses lignes n'a été écrite" in _texte(sortie)


def test_la_simulation_dit_la_decision_les_comptes_et_comment_ecrire(sortie, contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie)
    texte = _texte(sortie)

    assert f"Passage de {livraison.nom} sur le catalogue {PLAYGROUND}." in texte
    assert "Statut : ACCEPTEE_AVEC_REJETS" in texte
    assert "Lignes : 100 reçues, 98 valides, 2 rejetées" in texte
    assert "Alertes : HORS_PLAGE" in texte
    assert "Couches : bronze 100, coupées 0, silver 98, quarantaine 2" in texte
    assert "Simulation : rien n'a été créé ni écrit. Relancer avec --ecrire pour écrire dans les tables." in texte


def test_une_livraison_saine_n_a_aucune_alerte(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie)

    assert "Alertes : aucune" in _texte(sortie)


def test_une_livraison_bloquee_est_un_traitement_reussi_avec_son_statut_et_ses_alertes(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "usa-ca-001-bloquee", tmp_path)

    code = _lancer(chemin, FauxExecuteur(), sortie)
    texte = _texte(sortie)

    assert code == 0 and "Statut : BLOQUEE" in texte and "LIVRAISON_BLOQUEE" in texte


def test_un_nom_de_fichier_invalide_est_refuse_par_le_pipeline_pas_par_le_lanceur(sortie, tmp_path):
    chemin = tmp_path / "nom_invalide.csv"
    chemin.write_text("x\n", encoding="utf-8")

    code = _lancer(chemin, FauxExecuteur(), sortie)

    assert code == 0 and "Statut : REFUSEE" in _texte(sortie) and "NOM_FICHIER_INVALIDE" in _texte(sortie)


def test_la_sortie_ne_montre_aucune_valeur_de_ligne(sortie, contrat, livraisons, tmp_path):
    """Ni identifiant de lot, ni contenu : seulement des comptes et des codes. Le terminal peut être collé dans une PR publique."""
    chemin, _ = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie, "--ecrire")
    texte = _texte(sortie)

    ligne_de_donnees = chemin.read_text(encoding="utf-8").splitlines()[1]
    valeurs = re.findall(r"LOT-\d{6}|\d{4}-\d{2}-\d{2}", ligne_de_donnees)
    assert valeurs and "LOT-" not in texte
    assert not [valeur for valeur in valeurs if valeur in texte.replace(chemin.name, "")]


# ==============================================================================
# 4. ✍️ L'ÉCRITURE : le drapeau, l'ordre
# ==============================================================================
def test_avec_le_drapeau_les_schemas_et_les_tables_sont_crees_avant_toute_ecriture(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")
    textes = executeur.textes

    creations = [i for i, sql in enumerate(textes) if sql.lstrip().upper().startswith("CREATE")]
    fusions = [i for i, sql in enumerate(textes) if sql.lstrip().upper().startswith("MERGE")]
    assert len([i for i in creations if "CREATE SCHEMA" in textes[i]]) == 3
    assert len([i for i in creations if "CREATE TABLE" in textes[i]]) == 5
    assert fusions and max(creations) < min(fusions)


def test_le_registre_est_ecrit_en_dernier(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "deu-ba-001-avec-rejets", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")
    fusions = [sql for sql in executeur.textes if sql.lstrip().upper().startswith("MERGE")]

    assert "`ops`.`registre`" in fusions[-1]
    assert sum("`ops`.`registre`" in sql for sql in fusions) == 1
    assert any("`silver`.`lignes`" in sql for sql in fusions) and any("`silver`.`quarantaine`" in sql for sql in fusions)


def test_une_livraison_refusee_n_ecrit_que_son_registre_en_sql(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-004-colonne-manquante", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")
    fusions = [sql for sql in executeur.textes if sql.lstrip().upper().startswith("MERGE")]

    assert len(fusions) == 1 and "`ops`.`registre`" in fusions[0]


def test_la_lecture_de_l_etat_vient_avant_les_creations(sortie, contrat, livraisons, tmp_path):
    """On crée les tables d'abord, puis on lit leur état : sinon la première lecture échouerait sur des tables encore absentes."""
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")
    textes = executeur.textes

    premiere_lecture_de_table = next(i for i, sql in enumerate(textes) if "FROM `" in sql)
    derniere_creation = max(i for i, sql in enumerate(textes) if sql.lstrip().upper().startswith("CREATE"))
    assert derniere_creation < premiere_lecture_de_table


def test_la_sortie_dit_ce_qui_a_ete_ecrit_et_l_ordre(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    _lancer(chemin, FauxExecuteur(), sortie, "--ecrire")
    texte = _texte(sortie)

    assert f"Écrit dans {PLAYGROUND} : les couches de données d'abord, le registre en dernier." in texte
    assert "Simulation" not in texte


def test_les_requetes_d_ecriture_ne_contiennent_aucune_valeur_de_ligne_dans_leur_texte(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    _lancer(chemin, executeur, sortie, "--ecrire")

    assert not any("LOT-" in sql for sql in executeur.textes)
    assert any("LOT-" in str(parametres) for _, parametres in executeur.envoyes)          # elles voyagent dans le paramètre


# ==============================================================================
# 5. 💥 LES PANNES, LES INTERRUPTIONS
# ==============================================================================
def test_une_connexion_impossible_sort_avec_deux_et_dit_quoi_verifier(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("SELECT 1", PermissionError("jeton expiré"))])

    code = _lancer(chemin, executeur, sortie)
    texte = _texte(sortie)

    assert code == 2 and "Connexion impossible" in texte and "PermissionError" in texte
    assert "À vérifier" in texte and len(executeur.envoyes) == 1


def test_un_sdk_absent_dit_comment_l_installer_sans_pister_le_profil_ni_le_quota(monkeypatch, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)
    argv = ["--profil", "p", "--entrepot", "e", "--fichier", str(chemin), *CLES_DE_LIGNE]

    code = passage.principal(argv, sortie=sortie.append, maintenant=lambda: MAINTENANT)         # la fabrique réelle, pas un faux

    assert code == 2 and 'pip install -e ".[databricks]"' in _texte(sortie)
    assert "À vérifier" not in _texte(sortie)


def test_une_lecture_de_l_etat_qui_echoue_sort_avec_un_et_dit_que_ecrire_cree_les_tables(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("FROM `", RuntimeError("table introuvable"))])

    code = _lancer(chemin, executeur, sortie)
    texte = _texte(sortie)

    assert code == 1 and "Lecture de l'état impossible" in texte and "RuntimeError" in texte
    assert "--ecrire" in texte and "tables" in texte


def test_une_ecriture_qui_echoue_sort_avec_un_et_dit_comment_reprendre(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("MERGE INTO `rd_manuf_playground`.`silver`.`lignes`", ConnectionError("réseau"))])

    code = _lancer(chemin, executeur, sortie, "--ecrire", "--heure-arrivee", "2026-11-12T09:00:00Z")
    texte = _texte(sortie)

    assert code == 1 and "Écriture impossible" in texte and "ConnectionError" in texte
    assert "--heure-arrivee 2026-11-12T09:00:00Z" in texte and "sans rien dupliquer" in texte
    fusions = [sql for sql in executeur.textes if sql.lstrip().upper().startswith("MERGE")]
    assert not any("`ops`.`registre`" in sql for sql in fusions)                 # le registre, jamais écrit


def test_une_creation_de_tables_qui_echoue_sort_avec_un_et_n_ecrit_rien(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("CREATE TABLE", PermissionError("droit manquant"))])

    code = _lancer(chemin, executeur, sortie, "--ecrire")

    assert code == 1 and "Création des tables impossible" in _texte(sortie) and "PermissionError" in _texte(sortie)
    assert not any(sql.lstrip().upper().startswith("MERGE") for sql in executeur.textes)


def test_un_traitement_qui_echoue_sort_avec_un_et_n_ecrit_rien(monkeypatch, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur()

    def casse(*_args, **_kwargs):
        raise RuntimeError("fichier illisible")

    monkeypatch.setattr(passage, "traiter_livraison", casse)
    code = _lancer(chemin, executeur, sortie, "--ecrire")

    assert code == 1 and "Traitement de la livraison impossible" in _texte(sortie) and "RuntimeError" in _texte(sortie)
    assert not any(sql.lstrip().upper().startswith("MERGE") for sql in executeur.textes)


def test_l_heure_de_reprise_garde_les_microsecondes(sortie, contrat, livraisons, tmp_path):
    """Sans elles, la reprise aurait une autre clé de registre et ne reprendrait rien : elle écrirait une seconde livraison."""
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("MERGE INTO `rd_manuf_playground`.`silver`.`lignes`", ConnectionError("réseau"))])

    _lancer(chemin, executeur, sortie, "--ecrire", "--heure-arrivee", "2026-11-12T09:00:00.123456+00:00")

    assert "--heure-arrivee 2026-11-12T09:00:00.123456Z" in _texte(sortie)


def test_une_interruption_pendant_la_connexion_sort_avec_130(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    def interrompue(profil, entrepot, attente_max=None):
        raise KeyboardInterrupt

    try:
        code = _lancer(chemin, None, sortie, fabriquer=interrompue)
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")

    assert code == 130 and "Interrompu" in _texte(sortie) and "Connexion impossible" not in _texte(sortie)


def test_une_interruption_pendant_la_creation_dit_qu_aucune_ligne_n_a_ete_ecrite(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("CREATE SCHEMA", KeyboardInterrupt())])

    try:
        code = _lancer(chemin, executeur, sortie, "--ecrire")
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")

    assert code == 130 and "aucune de ses lignes n'a été écrite" in _texte(sortie)
    assert "Simulation" not in _texte(sortie)


def test_une_interruption_pendant_les_controles_locaux_est_rattrapee(monkeypatch, sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    def interrompu(_chemin):
        raise KeyboardInterrupt

    monkeypatch.setattr(passage, "charger_contrat", interrompu)
    try:
        code = _lancer(chemin, None, sortie, fabriquer=_jamais_appele)
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")

    assert code == 130 and "Interrompu" in _texte(sortie)


def test_un_message_d_erreur_est_borne(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("FROM `", RuntimeError("x" * 5000))])

    _lancer(chemin, executeur, sortie)

    assert max(len(ligne) for ligne in sortie) < 600


def test_une_interruption_pendant_la_lecture_sort_avec_130_sans_se_dire_une_panne(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("FROM `", KeyboardInterrupt())])

    try:
        code = _lancer(chemin, executeur, sortie)
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")

    assert code == 130 and "Interrompu" in _texte(sortie) and "impossible" not in _texte(sortie)


def test_une_interruption_pendant_l_ecriture_dit_comment_reprendre(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    executeur = FauxExecuteur(pannes=[("MERGE INTO `rd_manuf_playground`.`bronze`.`lignes`", KeyboardInterrupt())])

    try:
        code = _lancer(chemin, executeur, sortie, "--ecrire", "--heure-arrivee", "2026-11-12T09:00:00Z")
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")
    texte = _texte(sortie)

    assert code == 130 and "Interrompu" in texte
    assert "--heure-arrivee 2026-11-12T09:00:00Z" in texte and "sans rien dupliquer" in texte


def test_une_interruption_pendant_une_simulation_dit_que_rien_n_a_ete_ecrit(sortie, contrat, livraisons, tmp_path):
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    code = _lancer(chemin, FauxExecuteur(pannes=[("FROM `", KeyboardInterrupt())]), sortie)

    assert code == 130 and "Rien n'a été créé ni écrit" in _texte(sortie)


def test_le_programme_n_ecrit_aucun_fichier_meme_avec_le_drapeau(monkeypatch, sortie, contrat, livraisons, tmp_path):
    (tmp_path / "depot").mkdir()
    chemin, _ = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path / "depot")
    travail = tmp_path / "travail"
    travail.mkdir()
    monkeypatch.chdir(travail)

    _lancer(chemin, FauxExecuteur(), sortie, "--ecrire")

    assert list(travail.iterdir()) == []


def test_le_resultat_du_passage_est_immuable(contrat, livraisons, tmp_path):
    chemin, livraison = _fichier(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    resultat = passage.passer(contrat, _alertes(contrat), chemin, MagasinEnMemoire(), livraison.arrivee, ecrire=False)

    with pytest.raises(AttributeError):
        resultat.ecrit = True


# ==============================================================================
# 6. 🔒 LE MODULE ET SA DOCUMENTATION
# ==============================================================================
def test_importer_le_module_n_importe_pas_le_sdk():
    code = "import sys, rd_manuf.passage; sys.exit(1 if any(m == 'databricks' or m.startswith('databricks.') for m in sys.modules) else 0)"

    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_le_module_se_lance_en_programme_et_affiche_son_aide():
    resultat = subprocess.run([sys.executable, "-m", "rd_manuf.passage", "--help"], capture_output=True, text=True, check=False)

    assert resultat.returncode == 0 and "--ecrire" in resultat.stdout and "--fichier" in resultat.stdout


def test_le_readme_documente_la_commande_et_le_drapeau():
    readme = (RACINE / "README.md").read_text(encoding="utf-8")

    assert "python -m rd_manuf.passage" in readme and "--ecrire" in readme
