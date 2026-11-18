"""L'INGESTION INCRÉMENTALE : parmi les fichiers du dossier d'arrivée, ne traiter que ceux que le registre n'a pas déjà vus.

La logique (`ingerer`) travaille sur un MAGASIN quelconque, comme `passer` : les tests l'essaient avec le magasin en mémoire, qui garde
l'état d'un appel à l'autre, donc on prouve pour de vrai « traité une fois, puis plus rien », le renvoi corrigé, le conflit et la reprise.
Le dossier est un dossier ordinaire (`tmp_path`) rempli avec les fichiers de la journée de test.

Le module est importé par son nom (`ingestion.ingerer`) pour qu'un module encore vide fasse échouer chaque test sur son assertion, et non
la collecte de tout le fichier.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from appels_ast import appels_du_module

from rd_manuf import ingestion
from rd_manuf.alertes import charger_catalogue
from rd_manuf.contrat import StatutLivraison
from rd_manuf.ecriture import EtatMagasin, MagasinEnMemoire
from rd_manuf.journee import journee_multi_usines

RACINE = Path(__file__).resolve().parents[2]
CHEMIN_ALERTES = RACINE / "contracts" / "alertes" / "catalogue.yaml"
H1 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
H2, H3, H4 = H1 + timedelta(hours=1), H1 + timedelta(hours=2), H1 + timedelta(hours=3)


@pytest.fixture(scope="module")
def livraisons(contrat):
    return journee_multi_usines(contrat)


@pytest.fixture
def alertes():
    return charger_catalogue(CHEMIN_ALERTES)


def _deposer(contrat, livraisons, etiquette, dossier):
    """Dépose le fichier d'une livraison de la journée dans le dossier ; un fichier de même nom est remplacé, comme un renvoi."""
    livraison = next(livraison for livraison in livraisons if livraison.etiquette == etiquette)
    chemin = Path(dossier) / livraison.nom
    livraison.ecrire(contrat, chemin)
    return chemin


def _ingerer(contrat, alertes, dossier, magasin, heure, **options):
    options.setdefault("ecrire", True)
    return ingestion.ingerer(contrat, alertes, dossier, magasin, heure, **options)


def _statuts(resultat):
    return [passage.resultat.ligne_registre.statut for passage in resultat.passages]


def _registre(contrat, magasin):
    return EtatMagasin(contrat, magasin).registre


# ==============================================================================
# 1. 🆕 CE QUI EST NOUVEAU : un fichier que le registre n'a pas déjà vu
# ==============================================================================
def test_un_dossier_vide_n_a_rien_a_traiter(contrat, alertes, tmp_path):
    resultat = _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), H1)

    assert resultat.passages == () and resultat.selection.a_traiter == () and resultat.selection.deja_vus == ()


def test_un_dossier_introuvable_est_une_erreur_de_l_appelant(contrat, alertes, tmp_path):
    with pytest.raises(FileNotFoundError):
        _ingerer(contrat, alertes, tmp_path / "absent", MagasinEnMemoire(), H1)


def test_un_nouveau_fichier_est_traite_une_fois_et_relance_il_n_ajoute_rien_pas_meme_un_rejeu(contrat, livraisons, alertes, tmp_path):
    chemin = _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()

    premier = _ingerer(contrat, alertes, tmp_path, magasin, H1)
    second = _ingerer(contrat, alertes, tmp_path, magasin, H2)

    assert _statuts(premier) == [StatutLivraison.ACCEPTEE.value] and premier.selection.a_traiter == (chemin,)
    assert second.passages == () and second.selection.deja_vus == (chemin,)
    assert len(_registre(contrat, magasin)) == 1                     # `passer` aurait ajouté une ligne de rejeu à chaque lancement


def test_les_nouveaux_fichiers_sont_traites_dans_l_ordre_des_noms_comme_le_classement_du_silver(contrat, livraisons, alertes, tmp_path):
    for etiquette in ("usa-cb-001-memes-numeros-autre-usine", "fra-aa-002-parquet-saine", "deu-ba-001-avec-rejets", "fra-aa-001-saine"):
        _deposer(contrat, livraisons, etiquette, tmp_path)

    resultat = _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), H1)

    noms = [passage.resultat.ligne_registre.livraison_id for passage in resultat.passages]
    assert len(noms) == 4 and noms == sorted(noms)


def test_toute_la_journee_est_traitee_une_fois_puis_plus_rien(contrat, livraisons, alertes, tmp_path):
    premiers = {}
    for livraison in livraisons:
        premiers.setdefault(livraison.nom, livraison)               # une livraison par nom : les renvois et les rejeux reprennent un nom
    for livraison in premiers.values():
        livraison.ecrire(contrat, tmp_path / livraison.nom)
    magasin = MagasinEnMemoire()

    premier = _ingerer(contrat, alertes, tmp_path, magasin, H1)
    apres_le_premier = _registre(contrat, magasin)
    second = _ingerer(contrat, alertes, tmp_path, magasin, H2)

    assert len(premier.passages) == len(premiers) == len(apres_le_premier)
    assert second.passages == () and len(second.selection.deja_vus) == len(premiers)
    assert _registre(contrat, magasin) == apres_le_premier


def test_les_sous_dossiers_et_les_fichiers_caches_sont_ignores(contrat, livraisons, alertes, tmp_path):
    chemin = _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / ".depot_en_cours").write_text("pas une livraison", encoding="utf-8")

    resultat = _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), H1)

    assert resultat.selection.a_traiter == (chemin,) and resultat.selection.deja_vus == ()


# ==============================================================================
# 2. 🔁 LE MÊME NOM, UN AUTRE CONTENU : renvoi corrigé et conflit
# ==============================================================================
def test_un_fichier_refuse_ou_bloque_est_vu_une_fois_et_son_renvoi_corrige_est_traite_de_nouveau(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "usa-ca-001-bloquee", tmp_path)
    magasin = MagasinEnMemoire()

    bloque = _ingerer(contrat, alertes, tmp_path, magasin, H1)
    inchange = _ingerer(contrat, alertes, tmp_path, magasin, H2)
    _deposer(contrat, livraisons, "usa-ca-001-renvoyee-corrigee", tmp_path)                 # même nom, contenu corrigé
    corrige = _ingerer(contrat, alertes, tmp_path, magasin, H3)
    apres = _ingerer(contrat, alertes, tmp_path, magasin, H4)

    assert _statuts(bloque) == [StatutLivraison.BLOQUEE.value] and inchange.passages == ()
    assert _statuts(corrige) == [StatutLivraison.ACCEPTEE.value] and apres.passages == ()
    assert [ligne.statut for ligne in _registre(contrat, magasin)] == [StatutLivraison.BLOQUEE.value, StatutLivraison.ACCEPTEE.value]


def test_un_fichier_integre_renvoye_avec_un_autre_contenu_est_un_conflit_note_une_seule_fois(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()
    _ingerer(contrat, alertes, tmp_path, magasin, H1)
    _deposer(contrat, livraisons, "fra-aa-001-conflit-de-livraison", tmp_path)

    conflit = _ingerer(contrat, alertes, tmp_path, magasin, H2)
    apres = _ingerer(contrat, alertes, tmp_path, magasin, H3)

    assert _statuts(conflit) == [StatutLivraison.REFUSEE.value] and apres.passages == ()
    assert len(_registre(contrat, magasin)) == 2


def test_un_nom_invalide_est_refuse_une_fois_puis_il_est_vu_sans_etre_relu(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "nom-invalide", tmp_path)
    magasin = MagasinEnMemoire()

    premier = _ingerer(contrat, alertes, tmp_path, magasin, H1)
    second = _ingerer(contrat, alertes, tmp_path, magasin, H2)

    assert _statuts(premier) == [StatutLivraison.REFUSEE.value] and second.passages == ()
    assert [ligne.empreinte for ligne in _registre(contrat, magasin)] == [None]       # le fichier n'a pas été lu, il n'a pas d'empreinte


def test_la_selection_ne_lit_que_les_fichiers_dont_le_nom_est_deja_au_registre(monkeypatch, contrat, livraisons, alertes, tmp_path):
    """Hacher tout un dossier à chaque lancement le ferait grossir sans fin : seul un nom déjà connu demande de comparer le contenu."""
    _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()
    lus = []
    vrai = ingestion.empreinte_du_fichier
    monkeypatch.setattr(ingestion, "empreinte_du_fichier", lambda chemin: lus.append(chemin) or vrai(chemin))

    _ingerer(contrat, alertes, tmp_path, magasin, H1)
    assert lus == []                                                  # nom inconnu : nouveau, sans rien lire pour le décider

    _ingerer(contrat, alertes, tmp_path, magasin, H2)
    assert len(lus) == 1                                              # nom connu : un seul fichier lu, pour comparer


# ==============================================================================
# 3. 🧪 SIMULATION, LECTURE DONNÉE, PASSAGE PAR `passer`
# ==============================================================================
def test_une_simulation_n_ecrit_rien_donc_rien_n_est_vu_et_tout_reste_a_traiter(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    magasin = MagasinEnMemoire()

    premiere = _ingerer(contrat, alertes, tmp_path, magasin, H1, ecrire=False)
    seconde = _ingerer(contrat, alertes, tmp_path, magasin, H2, ecrire=False)

    assert len(premiere.passages) == len(seconde.passages) == 1 and premiere.ecrit is False
    assert _registre(contrat, magasin) == []


def test_l_etat_donne_a_la_place_de_celui_du_magasin_decide_de_ce_qui_est_deja_vu(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)
    ecrit = MagasinEnMemoire()
    _ingerer(contrat, alertes, tmp_path, ecrit, H1)
    etat = EtatMagasin(contrat, ecrit)

    resultat = _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), H2, ecrire=False, lire=lambda: (etat.registre, etat.lignes_silver))

    assert resultat.passages == () and len(resultat.selection.deja_vus) == 1


@pytest.mark.parametrize("ecrire", [False, True], ids=["simulation", "ecrire"])
def test_l_ingestion_passe_par_passer_avec_le_drapeau_qu_on_lui_donne(monkeypatch, ecrire, contrat, livraisons, alertes, tmp_path):
    for etiquette in ("fra-aa-001-saine", "deu-ba-001-avec-rejets"):
        _deposer(contrat, livraisons, etiquette, tmp_path)
    appels = []
    vrai = ingestion.passer

    def espion(*args, **kwargs):
        appels.append((Path(args[2]).name, kwargs["ecrire"]))
        return vrai(*args, **kwargs)

    monkeypatch.setattr(ingestion, "passer", espion)

    _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), H1, ecrire=ecrire)

    assert [drapeau for _, drapeau in appels] == [ecrire, ecrire] and len(appels) == 2


# La séquence « lire l'état, traiter, écrire » vit UNE fois, dans `passer` : l'ingestion l'appelle pour chaque fichier. Si elle en
# réécrivait une étape, les deux copies pourraient dériver sans qu'aucun test le voie (comme le programme de passage avant la #55).
ETAPES_DE_PASSER = {"lire_l_etat", "traiter_livraison", "ecrire_le_resultat"}


def _etapes_appelees(source):
    """Les étapes de `passer` que le code appelle lui-même : par leur nom, par un attribut (`module.etape(...)`) ou par un alias."""
    return appels_du_module(source) & ETAPES_DE_PASSER


@pytest.mark.parametrize(
    ("source", "attendu"),
    [
        ("from m import passer\ndef f():\n    return passer(a)\n", set()),
        ("from m import traiter_livraison\ndef f():\n    return traiter_livraison(a)\n", {"traiter_livraison"}),  # par son nom
        ("from m import ecrire_le_resultat as ecrire\ndef f():\n    return ecrire(a)\n", {"ecrire_le_resultat"}),  # un alias d'import
        ("def f():\n    t = traiter_livraison\n    return t(a)\n", {"traiter_livraison"}),  # un alias d'affectation
        ("from rd_manuf import pipeline\ndef f():\n    return pipeline.traiter_livraison(a)\n", {"traiter_livraison"}),  # un attribut
        ("from rd_manuf import passage\ndef f():\n    return passage.ecrire_le_resultat(a)\n", {"ecrire_le_resultat"}),  # un autre
        ("def f():\n    def g():\n        return lire_l_etat(a)\n    return g()\n", {"lire_l_etat"}),  # imbriqué
        ("traiter_livraison(a)\n", {"traiter_livraison"}),  # hors fonction
    ],
    ids=["propre", "nom", "alias d'import", "alias d'affectation", "attribut pipeline", "attribut passage", "imbriqué", "hors fonction"],
)
def test_le_garde_de_l_ingestion_voit_les_etapes_de_passer_ou_qu_elles_soient_appelees(source, attendu):
    assert _etapes_appelees(source) == attendu


def test_l_ingestion_appelle_passer_et_ne_reecrit_aucune_de_ses_etapes():
    source = Path(ingestion.__file__).read_text(encoding="utf-8")

    assert "passer" in appels_du_module(source), "l'ingestion doit appeler `passer`"
    assert _etapes_appelees(source) == set(), f"l'ingestion réécrit des étapes de `passer` : {sorted(_etapes_appelees(source))}"


def test_une_heure_sans_fuseau_est_refusee_comme_pour_un_passage(contrat, livraisons, alertes, tmp_path):
    _deposer(contrat, livraisons, "fra-aa-001-saine", tmp_path)

    with pytest.raises(ValueError, match="fuseau"):
        _ingerer(contrat, alertes, tmp_path, MagasinEnMemoire(), datetime(2026, 11, 15, 8, 0))


# ==============================================================================
# 4. 🔌 UNE PANNE ARRÊTE LE LOT ; LA REPRISE, À LA MÊME HEURE, DONNE LE MÊME ÉTAT QU'UN LOT SANS PANNE
# ==============================================================================
class MagasinQuiLacheLeRegistre(MagasinEnMemoire):
    """Tombe en panne UNE fois, au moment d'écrire la ligne de registre d'une livraison : ses données sont écrites, sa décision non."""

    def __init__(self, livraison_id):
        super().__init__()
        self.livraison_id = livraison_id
        self.a_lache = False

    def inserer_absentes(self, table, cles, enregistrements):
        if table.nom == "registre" and not self.a_lache and any(e["livraison_id"] == self.livraison_id for e in enregistrements):
            self.a_lache = True
            raise OSError("panne du service")
        super().inserer_absentes(table, cles, enregistrements)


def test_une_panne_arrete_le_lot_et_la_reprise_a_la_meme_heure_donne_l_etat_d_un_lot_sans_panne(contrat, livraisons, alertes, tmp_path):
    for etiquette in ("fra-aa-001-saine", "fra-aa-002-parquet-saine", "deu-ba-001-avec-rejets"):
        _deposer(contrat, livraisons, etiquette, tmp_path)
    sans_panne = MagasinEnMemoire()
    _ingerer(contrat, alertes, tmp_path, sans_panne, H1)
    nom_du_milieu = sorted(chemin.name for chemin in tmp_path.iterdir())[1]
    magasin = MagasinQuiLacheLeRegistre(nom_du_milieu)

    with pytest.raises(OSError, match="panne du service"):
        _ingerer(contrat, alertes, tmp_path, magasin, H1)
    assert len(_registre(contrat, magasin)) == 1                      # la première est écrite, celle de la panne n'a pas sa décision
    reprise = _ingerer(contrat, alertes, tmp_path, magasin, H1)

    assert len(reprise.selection.deja_vus) == 1 and len(reprise.passages) == 2        # la première est vue, les deux autres refaites
    assert _registre(contrat, magasin) == _registre(contrat, sans_panne)
    assert EtatMagasin(contrat, magasin).lignes_silver == EtatMagasin(contrat, sans_panne).lignes_silver
