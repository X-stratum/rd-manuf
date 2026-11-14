"""Tests des DÉFAUTS DE FORME du simulateur : des fichiers abîmés comme une usine pourrait les envoyer, demandés par défaut.

Les défauts de valeur (`HORS_PLAGE`, ...) vivent dans `generer_livraison` ; ceux-ci vivent dans l'écriture du fichier : une colonne en trop,
un fichier vide, une ligne coupée. Ce n'est pas le simulateur qui juge : l'ORACLE est la réception (`receptionner_fichier`) puis
l'enchaînement complet (`traiter_livraison`). Le simulateur dit « j'ai coupé 2 lignes », la réception doit en trouver exactement 2.
Un défaut qui n'existe pas dans un format (une ligne coupée en Parquet) est refusé, comme `TYPE_INVALIDE` en Parquet.
"""
import copy
import csv
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rd_manuf.alertes import charger_catalogue
from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.lecture import lire_csv, lire_selon_format
from rd_manuf.pipeline import traiter_livraison
from rd_manuf.reception import receptionner_fichier
from rd_manuf.silver import ResultatSilver
from rd_manuf.simulateur import (
    DefautDeFichier,
    ecrire_livraison_csv,
    ecrire_livraison_parquet,
    generer_livraison,
    nom_de_fichier,
)
from rd_manuf.structure import CodeStructure

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
GRAINE = 42
NB_LIGNES = 20
INSTANT = datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC)
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
FORMATS = ["csv", "parquet"]
NOMS = {format: nom_de_fichier("FRA", "AA", "001", INSTANT, format) for format in FORMATS}
ECRIVAINS = {"csv": ecrire_livraison_csv, "parquet": ecrire_livraison_parquet}
RIEN = ResultatSilver(lignes=[], quarantaine=[])

D = DefautDeFichier
# Ce que la réception doit répondre pour chaque défaut qui refuse la livraison entière.
CODE_ATTENDU = {
    D.COLONNE_MANQUANTE: CodeStructure.COLONNE_MANQUANTE,
    D.COLONNE_INCONNUE: CodeStructure.COLONNE_INCONNUE,
    D.COLONNE_DUPLIQUEE: CodeStructure.COLONNE_DUPLIQUEE,
    D.FICHIER_VIDE: CodeStructure.FICHIER_VIDE,
    D.FICHIER_ILLISIBLE: CodeStructure.FICHIER_ILLISIBLE,
}
DEFAUTS_DE_COLONNE = [D.COLONNE_MANQUANTE, D.COLONNE_INCONNUE, D.COLONNE_DUPLIQUEE]
# Les cinq défauts qui refusent la livraison, dans les formats où ils existent (un Parquet de 0 octet n'est pas un Parquet vide : il est
# illisible ; un Parquet vide est un Parquet sans ligne, que `lignes=[]` fabrique déjà).
REFUS = [(format, defaut) for format in FORMATS for defaut in CODE_ATTENDU if (format, defaut) != ("parquet", D.FICHIER_VIDE)]
FORMAT_ET_DEFAUT = pytest.mark.parametrize(
    ("format", "defaut"), REFUS, ids=[f"{format}-{defaut.value}" for format, defaut in REFUS]
)


@pytest.fixture(scope="module")
def catalogue():
    return charger_catalogue(CHEMIN_CATALOGUE)


def _ecrire(tmp_path, contrat, format, defauts=None, nb_lignes=NB_LIGNES, anomalies=None, nom=None):
    chemin = tmp_path / (nom or NOMS[format])
    lignes = generer_livraison(contrat, nb_lignes, GRAINE, anomalies)
    ECRIVAINS[format](contrat, lignes, chemin, defauts=defauts)
    return chemin


def _codes(reception):
    return [probleme.code for probleme in reception.problemes_structure]


def _noms(contrat):
    return [colonne.nom for colonne in contrat.colonnes]


# ==============================================================================
# 1. 🏛️ UN DÉFAUT DE COLONNE : la livraison est refusée, avec son seul code et le bon nombre de colonnes
# ==============================================================================
@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("defaut", DEFAUTS_DE_COLONNE, ids=lambda defaut: defaut.value)
@pytest.mark.parametrize("nombre", [1, 2])
def test_un_defaut_de_colonne_refuse_la_livraison_avec_son_seul_code(tmp_path, contrat, format, defaut, nombre):
    chemin = _ecrire(tmp_path, contrat, format, defauts={defaut: nombre})

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.REFUSEE
    assert _codes(reception) == [CODE_ATTENDU[defaut]]
    assert len(reception.problemes_structure[0].colonnes) == nombre
    assert reception.nb_recues == NB_LIGNES            # les lignes sont arrivées, même si aucune n'est lue


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("nombre", [1, 2])
def test_des_colonnes_manquantes_sont_retirees_de_l_en_tete_et_de_chaque_ligne(tmp_path, contrat, format, nombre):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.COLONNE_MANQUANTE: nombre})

    fichier = lire_selon_format(chemin, format)

    retirees = set(_noms(contrat)) - set(fichier.colonnes)
    assert len(retirees) == nombre
    assert len(fichier.colonnes) == len(_noms(contrat)) - nombre and set(fichier.colonnes) <= set(_noms(contrat))
    assert all(set(ligne) == set(fichier.colonnes) for ligne in fichier.lignes)


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("nombre", [1, 2])
def test_des_colonnes_inconnues_s_ajoutent_apres_celles_du_contrat(tmp_path, contrat, format, nombre):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.COLONNE_INCONNUE: nombre})

    colonnes = lire_selon_format(chemin, format).colonnes

    noms = _noms(contrat)
    assert colonnes[: len(noms)] == noms
    assert len(colonnes) == len(noms) + nombre
    ajoutees = colonnes[len(noms):]
    assert not set(ajoutees) & set(noms) and len(set(colonnes)) == len(colonnes)    # inconnues du contrat, distinctes entre elles


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("nombre", [1, 2])
def test_des_colonnes_dupliquees_apparaissent_exactement_deux_fois(tmp_path, contrat, format, nombre):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.COLONNE_DUPLIQUEE: nombre})

    compte = Counter(lire_selon_format(chemin, format).colonnes)

    assert set(compte) == set(_noms(contrat))
    assert sorted(compte.values()) == [1] * (len(_noms(contrat)) - nombre) + [2] * nombre


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("nombre", [1, 2])
def test_les_colonnes_manquantes_sont_les_dernieres_du_contrat(tmp_path, contrat, format, nombre):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.COLONNE_MANQUANTE: nombre})

    assert lire_selon_format(chemin, format).colonnes == _noms(contrat)[:-nombre]


@pytest.mark.parametrize("format", FORMATS)
def test_les_colonnes_dupliquees_sont_les_premieres_du_fichier_et_jamais_une_colonne_retiree(tmp_path, contrat, format):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.COLONNE_MANQUANTE: 2, D.COLONNE_DUPLIQUEE: 2})

    noms = _noms(contrat)
    assert lire_selon_format(chemin, format).colonnes == noms[:-2] + noms[:2]


@pytest.mark.parametrize("defaut", DEFAUTS_DE_COLONNE, ids=lambda defaut: defaut.value)
@pytest.mark.parametrize("nombre", [1, 2])
def test_le_csv_et_le_parquet_recoivent_la_meme_decision_pour_un_defaut_de_colonne(tmp_path, contrat, defaut, nombre):
    def decision(format):
        chemin = _ecrire(tmp_path, contrat, format, defauts={defaut: nombre}, nom=f"{format}-{NOMS[format]}")
        reception = receptionner_fichier(contrat, chemin)
        return reception.statut, [(p.code, len(p.colonnes)) for p in reception.problemes_structure], reception.nb_recues

    assert decision("csv") == decision("parquet")


def test_la_borne_haute_des_colonnes_manquantes_laisse_une_colonne(tmp_path, contrat):
    n = len(contrat.colonnes)

    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.COLONNE_MANQUANTE: n - 1})

    assert len(lire_csv(chemin).colonnes) == 1
    assert _codes(receptionner_fichier(contrat, chemin)) == [CodeStructure.COLONNE_MANQUANTE]


def test_toutes_les_colonnes_peuvent_etre_dupliquees(tmp_path, contrat):
    n = len(contrat.colonnes)

    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.COLONNE_DUPLIQUEE: n})

    assert len(lire_csv(chemin).colonnes) == 2 * n
    assert len(receptionner_fichier(contrat, chemin).problemes_structure[0].colonnes) == n


def test_plusieurs_defauts_de_colonne_se_combinent_et_se_listent_tous(tmp_path, contrat):
    defauts = {D.COLONNE_MANQUANTE: 1, D.COLONNE_INCONNUE: 1, D.COLONNE_DUPLIQUEE: 1}

    reception = receptionner_fichier(contrat, _ecrire(tmp_path, contrat, "csv", defauts=defauts))

    assert _codes(reception) == sorted(CODE_ATTENDU[defaut] for defaut in defauts)     # `controler_structure` les trie par code


# ==============================================================================
# 2. 📭 UN FICHIER VIDE ET UN FICHIER ILLISIBLE
# ==============================================================================
def test_un_csv_vide_n_a_pas_un_seul_octet_et_la_reception_le_dit(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.FICHIER_VIDE: 1})

    reception = receptionner_fichier(contrat, chemin)

    assert chemin.stat().st_size == 0
    assert (reception.statut, _codes(reception), reception.nb_recues) == (StatutLivraison.REFUSEE, [CodeStructure.FICHIER_VIDE], 0)


def test_un_csv_sans_ligne_n_est_pas_un_fichier_vide_au_sens_des_octets_mais_est_refuse_de_meme(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", nb_lignes=0)

    assert chemin.stat().st_size > 0                       # il reste l'en-tête
    assert _codes(receptionner_fichier(contrat, chemin)) == [CodeStructure.FICHIER_VIDE]


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("nb_lignes", [0, NB_LIGNES])
def test_un_fichier_illisible_est_refuse_comme_tel_avec_ou_sans_ligne(tmp_path, contrat, format, nb_lignes):
    chemin = _ecrire(tmp_path, contrat, format, defauts={D.FICHIER_ILLISIBLE: 1}, nb_lignes=nb_lignes)

    reception = receptionner_fichier(contrat, chemin)

    assert (reception.statut, _codes(reception)) == (StatutLivraison.REFUSEE, [CodeStructure.FICHIER_ILLISIBLE])


def test_un_csv_illisible_ne_se_decode_pas_en_utf8(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.FICHIER_ILLISIBLE: 1})

    with pytest.raises(UnicodeDecodeError):
        chemin.read_bytes().decode("utf-8-sig")


def test_un_parquet_illisible_est_un_parquet_tronque(tmp_path, contrat):
    sain = _ecrire(tmp_path, contrat, "parquet", nom=f"sain-{NOMS['parquet']}")
    chemin = _ecrire(tmp_path, contrat, "parquet", defauts={D.FICHIER_ILLISIBLE: 1})

    assert 0 < chemin.stat().st_size < sain.stat().st_size


# ==============================================================================
# 3. ✂️ UNE LIGNE COUPÉE (CSV seulement : un Parquet n'a pas de ligne « à moitié écrite »)
# ==============================================================================
@pytest.mark.parametrize("nombre", [1, 3])
def test_des_lignes_coupees_sont_recues_et_rejetees_comme_malformees(tmp_path, contrat_sans_seuil, nombre):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, "csv", defauts={D.LIGNE_COUPEE: nombre})

    reception = receptionner_fichier(contrat_sans_seuil, chemin)

    assert reception.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES, NB_LIGNES - nombre, nombre)
    violations = Counter(v.code for rejet in reception.resultat.lignes_rejetees for v in rejet.violations)
    assert violations == {CodeErreur.LIGNE_MALFORMEE: nombre}


@pytest.mark.parametrize("nombre", [1, 3])
def test_les_lignes_coupees_sont_les_dernieres_du_fichier_et_ont_moins_de_champs(tmp_path, contrat, nombre):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.LIGNE_COUPEE: nombre})

    fichier = lire_csv(chemin)

    assert len(fichier.lignes) == NB_LIGNES - nombre
    attendus = list(range(NB_LIGNES - nombre + 2, NB_LIGNES + 2))      # l'en-tête est la ligne 1
    assert [ligne.numero for ligne in fichier.lignes_malformees] == attendus
    assert all(0 < len(ligne.champs) < len(fichier.colonnes) for ligne in fichier.lignes_malformees)


def test_les_lignes_bien_formees_d_un_fichier_a_lignes_coupees_sont_celles_de_la_livraison(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.LIGNE_COUPEE: 2})
    attendues = generer_livraison(contrat, NB_LIGNES, GRAINE)[: NB_LIGNES - 2]

    relues = lire_csv(chemin).lignes

    assert [ligne["id_lot"] for ligne in relues] == [ligne["id_lot"] for ligne in attendues]


def test_une_seule_ligne_coupee_bloque_une_livraison_dont_le_seuil_est_a_zero(tmp_path, contrat_strict):
    reception = receptionner_fichier(contrat_strict, _ecrire(tmp_path, contrat_strict, "csv", defauts={D.LIGNE_COUPEE: 1}))

    assert reception.statut == StatutLivraison.BLOQUEE


def test_toutes_les_lignes_peuvent_etre_coupees(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.LIGNE_COUPEE: NB_LIGNES})

    fichier = lire_csv(chemin)
    reception = receptionner_fichier(contrat, chemin)

    assert (fichier.lignes, len(fichier.lignes_malformees)) == ([], NB_LIGNES)
    assert (reception.statut, reception.nb_recues, reception.nb_rejetees) == (StatutLivraison.BLOQUEE, NB_LIGNES, NB_LIGNES)


def test_couper_une_ligne_a_anomalie_la_remplace_et_seule_la_ligne_coupee_est_jugee(tmp_path, contrat_sans_seuil):
    """Les lignes coupées sont les DERNIÈRES, là où `generer_livraison` range ses anomalies : couper l'une efface son anomalie."""
    chemin = _ecrire(tmp_path, contrat_sans_seuil, "csv", defauts={D.LIGNE_COUPEE: 2}, anomalies={CodeErreur.HORS_PLAGE: 2})

    reception = receptionner_fichier(contrat_sans_seuil, chemin)

    violations = Counter(v.code for rejet in reception.resultat.lignes_rejetees for v in rejet.violations)
    assert violations == {CodeErreur.LIGNE_MALFORMEE: 2}


def test_une_ligne_coupee_suit_l_en_tete_du_fichier_et_non_celui_du_contrat(tmp_path, contrat_sans_seuil):
    defauts = {D.COLONNE_MANQUANTE: 1, D.LIGNE_COUPEE: 2}

    reception = receptionner_fichier(contrat_sans_seuil, _ecrire(tmp_path, contrat_sans_seuil, "csv", defauts=defauts))

    assert _codes(reception) == [CodeStructure.COLONNE_MANQUANTE]       # la structure est jugée avant les lignes
    assert reception.nb_recues == NB_LIGNES                             # et les lignes coupées comptent comme reçues


def test_une_ligne_ne_peut_pas_etre_coupee_s_il_n_y_a_qu_une_colonne(tmp_path, contrat):
    """Une ligne d'une seule colonne n'est jamais « malformée » : tronquée, elle serait vide ou entière."""
    chemin = tmp_path / NOMS["csv"]
    defauts = {D.COLONNE_MANQUANTE: len(contrat.colonnes) - 1, D.LIGNE_COUPEE: 1}

    with pytest.raises(ValueError, match="LIGNE_COUPEE"):
        ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin, defauts=defauts)
    assert not chemin.exists()


@pytest.mark.parametrize("autre", [D.COLONNE_INCONNUE, D.COLONNE_DUPLIQUEE], ids=lambda defaut: defaut.value)
def test_une_ligne_est_coupee_des_que_l_en_tete_a_deux_colonnes_quelle_qu_en_soit_l_origine(tmp_path, contrat, autre):
    """Un fichier à une colonne du contrat, plus une colonne inconnue (ou en double) : deux champs, la ligne coupée en garde un."""
    defauts = {D.COLONNE_MANQUANTE: len(contrat.colonnes) - 1, autre: 1, D.LIGNE_COUPEE: 1}

    fichier = lire_csv(_ecrire(tmp_path, contrat, "csv", defauts=defauts))

    assert len(fichier.colonnes) == 2
    assert [len(ligne.champs) for ligne in fichier.lignes_malformees] == [1]
    assert len(fichier.lignes) == NB_LIGNES - 1


@pytest.mark.parametrize("autre", [D.FICHIER_VIDE, D.FICHIER_ILLISIBLE], ids=lambda defaut: defaut.value)
def test_un_defaut_du_fichier_entier_accepte_les_autres_defauts_a_zero(tmp_path, contrat, autre):
    """Zéro veut dire « non demandé » : il ne compte pas parmi les défauts qui empêchent de demander celui-ci."""
    defauts = {defaut: 0 for defaut in D} | {autre: 1}

    chemin = _ecrire(tmp_path, contrat, "csv", defauts=defauts)

    assert _codes(receptionner_fichier(contrat, chemin)) == [CODE_ATTENDU[autre]]


def test_une_ligne_qui_porte_un_champ_inconnu_du_contrat_est_refusee_sans_ecrire_de_fichier(tmp_path, contrat):
    """Un champ que le contrat ne connaît pas serait perdu en silence : c'est une erreur de l'appelant, pas un défaut à fabriquer."""
    chemin = tmp_path / NOMS["csv"]
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    lignes[-1] = {**lignes[-1], "champ_en_trop": 1}

    with pytest.raises(ValueError, match="champ_en_trop"):
        ecrire_livraison_csv(contrat, lignes, chemin)
    assert not chemin.exists()


def test_une_cle_absente_d_une_ligne_s_ecrit_comme_un_champ_vide(tmp_path, contrat):
    chemin = tmp_path / NOMS["csv"]

    ecrire_livraison_csv(contrat, [{"id_lot": "LOT-000001"}], chemin)

    relue = lire_csv(chemin).lignes[0]
    assert relue["id_lot"] == "LOT-000001"
    assert {valeur for nom, valeur in relue.items() if nom != "id_lot"} == {""}


# ==============================================================================
# 4. 🚫 UNE DEMANDE IMPOSSIBLE EST REFUSÉE, SANS ÉCRIRE DE FICHIER
# ==============================================================================
def _colonnes(contrat):
    return len(contrat.colonnes)


def _doublons_apres_retrait(contrat):
    """Deux colonnes retirées : il en reste n - 2, et n - 1 doublons sont demandés (sous le total du contrat, au-dessus du reste)."""
    return {D.COLONNE_MANQUANTE: 2, D.COLONNE_DUPLIQUEE: _colonnes(contrat) - 1}


@pytest.mark.parametrize(
    ("format", "defauts", "fragment"),
    [
        pytest.param("parquet", lambda c: {D.LIGNE_COUPEE: 1}, "LIGNE_COUPEE", id="parquet-ligne-coupee"),
        pytest.param("parquet", lambda c: {D.FICHIER_VIDE: 1}, "FICHIER_VIDE", id="parquet-sans-octet"),
        pytest.param("csv", lambda c: {D.FICHIER_VIDE: 2}, "FICHIER_VIDE", id="csv-vide-deux-fois"),
        pytest.param("csv", lambda c: {D.FICHIER_ILLISIBLE: 2}, "FICHIER_ILLISIBLE", id="csv-illisible-deux-fois"),
        pytest.param("parquet", lambda c: {D.FICHIER_ILLISIBLE: 2}, "FICHIER_ILLISIBLE", id="parquet-illisible-deux-fois"),
        pytest.param("csv", lambda c: {D.FICHIER_VIDE: 1, D.COLONNE_MANQUANTE: 1}, "FICHIER_VIDE", id="csv-vide-et-autre"),
        pytest.param("csv", lambda c: {D.FICHIER_ILLISIBLE: 1, D.LIGNE_COUPEE: 1}, "FICHIER_ILLISIBLE", id="csv-illisible-et-autre"),
        pytest.param(
            "parquet", lambda c: {D.FICHIER_ILLISIBLE: 1, D.COLONNE_INCONNUE: 1}, "FICHIER_ILLISIBLE", id="parquet-illisible-et-autre"
        ),
        pytest.param("csv", lambda c: {D.COLONNE_MANQUANTE: _colonnes(c)}, "COLONNE_MANQUANTE", id="csv-toutes-manquantes"),
        pytest.param("parquet", lambda c: {D.COLONNE_MANQUANTE: _colonnes(c)}, "COLONNE_MANQUANTE", id="parquet-toutes-manquantes"),
        pytest.param("csv", lambda c: {D.COLONNE_DUPLIQUEE: _colonnes(c) + 1}, "COLONNE_DUPLIQUEE", id="csv-trop-de-doublons"),
        pytest.param("parquet", lambda c: {D.COLONNE_DUPLIQUEE: _colonnes(c) + 1}, "COLONNE_DUPLIQUEE", id="parquet-trop-de-doublons"),
        pytest.param("csv", _doublons_apres_retrait, "COLONNE_DUPLIQUEE", id="csv-doublons-trop-nombreux"),
        pytest.param("parquet", _doublons_apres_retrait, "COLONNE_DUPLIQUEE", id="parquet-doublons-trop"),
        pytest.param("csv", lambda c: {D.LIGNE_COUPEE: NB_LIGNES + 1}, "LIGNE_COUPEE", id="csv-plus-de-lignes-coupees-que-de-lignes"),
        pytest.param("csv", lambda c: {D.COLONNE_INCONNUE: -1}, "COLONNE_INCONNUE", id="csv-nombre-negatif"),
        pytest.param("parquet", lambda c: {D.COLONNE_INCONNUE: -1}, "COLONNE_INCONNUE", id="parquet-nombre-negatif"),
        pytest.param("csv", lambda c: {"COLONNE_INCONNUE": 1}, "inconnu", id="csv-un-texte-n-est-pas-un-defaut"),
        pytest.param("parquet", lambda c: {CodeErreur.HORS_PLAGE: 1}, "inconnu", id="parquet-un-code-d-erreur-n-est-pas-un-defaut"),
    ],
)
def test_une_demande_impossible_est_refusee_et_n_ecrit_rien(tmp_path, contrat, format, defauts, fragment):
    chemin = tmp_path / NOMS[format]
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)

    with pytest.raises(ValueError, match=fragment):
        ECRIVAINS[format](contrat, lignes, chemin, defauts=defauts(contrat))

    assert not chemin.exists()


# ==============================================================================
# 5. 🔁 SANS DÉFAUT, RIEN NE CHANGE ; AVEC UN DÉFAUT, LE FICHIER EST REPRODUCTIBLE
# ==============================================================================
@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize(
    "defauts",
    [None, {}, {defaut: 0 for defaut in D}],
    ids=["none", "vide", "zero-partout"],
)
def test_sans_defaut_demande_le_fichier_est_celui_d_avant(tmp_path, contrat, format, defauts):
    """Zéro veut dire « non demandé », même pour un défaut qui n'existe pas dans ce format (une ligne coupée en Parquet)."""
    sans = _ecrire(tmp_path, contrat, format, nom=f"sans-{NOMS[format]}")
    avec = _ecrire(tmp_path, contrat, format, defauts=defauts)

    assert avec.read_bytes() == sans.read_bytes()


@pytest.mark.parametrize(
    ("format", "defauts"),
    [
        pytest.param("csv", {D.COLONNE_MANQUANTE: 2, D.LIGNE_COUPEE: 3}, id="csv"),
        pytest.param("parquet", {D.COLONNE_MANQUANTE: 2, D.COLONNE_INCONNUE: 1}, id="parquet"),
    ],
)
def test_le_meme_defaut_donne_le_meme_fichier(tmp_path, contrat, format, defauts):
    premier = _ecrire(tmp_path, contrat, format, defauts=defauts, nom=f"1-{NOMS[format]}")
    second = _ecrire(tmp_path, contrat, format, defauts=defauts, nom=f"2-{NOMS[format]}")

    assert premier.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("format", FORMATS)
def test_l_ecriture_d_un_defaut_ne_modifie_ni_les_lignes_ni_les_defauts_recus(tmp_path, contrat, format):
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    avant = copy.deepcopy(lignes)
    defauts = {D.COLONNE_DUPLIQUEE: 1, D.COLONNE_INCONNUE: 1}
    defauts_avant = dict(defauts)

    ECRIVAINS[format](contrat, lignes, tmp_path / NOMS[format], defauts=defauts)

    assert lignes == avant and defauts == defauts_avant


def test_un_csv_a_defaut_de_colonne_a_des_lignes_de_la_largeur_de_son_en_tete(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, "csv", defauts={D.COLONNE_INCONNUE: 2, D.COLONNE_DUPLIQUEE: 1})

    with chemin.open(encoding="utf-8", newline="") as fichier:
        largeurs = {len(ligne) for ligne in csv.reader(fichier)}

    assert largeurs == {len(_noms(contrat)) + 3}       # chaque ligne a autant de champs que l'en-tête : seule la structure est fautive


# ==============================================================================
# 6. 🔗 DE BOUT EN BOUT : le registre, le bronze, le silver et les alertes voient le même défaut
# ==============================================================================
def _traiter(contrat, catalogue, chemin):
    return traiter_livraison(contrat, catalogue, chemin, (), (), ARRIVEE)


@FORMAT_ET_DEFAUT
def test_un_defaut_de_forme_refuse_la_livraison_sans_couche_et_alerte_l_emetteur(tmp_path, contrat, catalogue, format, defaut):
    resultat = _traiter(contrat, catalogue, _ecrire(tmp_path, contrat, format, defauts={defaut: 1}))

    assert resultat.ligne_registre.statut == "REFUSEE"
    assert (resultat.lignes_bronze, resultat.lignes_coupees, resultat.silver) == ([], [], RIEN)
    assert [alerte.code for alerte in resultat.alertes] == [CODE_ATTENDU[defaut].value]
    assert resultat.alertes[0].agit == "emetteur"


@pytest.mark.parametrize("nombre", [1, 2])
def test_des_lignes_coupees_entrent_au_bronze_a_part_et_en_quarantaine_au_silver(tmp_path, contrat_sans_seuil, catalogue, nombre):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, "csv", defauts={D.LIGNE_COUPEE: nombre})

    resultat = _traiter(contrat_sans_seuil, catalogue, chemin)

    assert resultat.ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"
    assert sorted(ligne.numero_fichier for ligne in resultat.lignes_coupees) == list(range(NB_LIGNES - nombre + 2, NB_LIGNES + 2))
    assert len(resultat.lignes_bronze) == len(resultat.silver.lignes) == NB_LIGNES - nombre      # le bronze met les coupées à part
    assert len(resultat.silver.quarantaine) == nombre
    assert [alerte.code for alerte in resultat.alertes] == ["LIGNE_MALFORMEE"]
    assert resultat.alertes[0].nb_lignes == nombre


def test_une_livraison_bloquee_par_une_ligne_coupee_la_garde_au_bronze_et_rien_au_silver(tmp_path, contrat_strict, catalogue):
    chemin = _ecrire(tmp_path, contrat_strict, "csv", defauts={D.LIGNE_COUPEE: 1})

    resultat = _traiter(contrat_strict, catalogue, chemin)

    assert resultat.ligne_registre.statut == "BLOQUEE"
    assert len(resultat.lignes_coupees) == 1 and len(resultat.lignes_bronze) == NB_LIGNES - 1
    assert resultat.silver == RIEN
