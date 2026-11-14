"""La SURVEILLANCE, premier axe : la FRAÎCHEUR. Une source qui n'envoie plus rien ne déclenche aucune alerte : le pipeline ne réagit qu'aux
fichiers qui arrivent. On déclare donc les sources attendues et le délai maximal entre deux livraisons, et une fonction pure compare le
registre à « maintenant » pour dire lesquelles sont en retard, lesquelles n'ont jamais livré, et quelles origines livrent sans être
déclarées.

Les valeurs attendues des premiers tests sont comptées ICI, à la main ; la journée multi-usines de la #41 sert de preuve d'ensemble, avec
ses attendus recalculés à part depuis la liste des livraisons, jamais lus dans le résultat du code. La surveillance ne route rien
vers des personnes : elle rend des constats (annuaire et envoi viendront ailleurs).

Le module est importé par son nom (`surveillance.controler_...`) pour qu'un module encore vide fasse échouer chaque test sur son assertion,
et non la collecte de tout le fichier.
"""
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rd_manuf import surveillance
from rd_manuf.alertes import charger_catalogue
from rd_manuf.contrat import StatutLivraison
from rd_manuf.journee import PREMIERE_ARRIVEE, Etat, jouer, journee_multi_usines
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.registre import STATUT_REJEU, LigneRegistre

RACINE = Path(__file__).resolve().parents[2]
CHEMIN_SOURCES = RACINE / "contracts" / "surveillance" / "sources_attendues.yaml"
CHEMIN_CATALOGUE = RACINE / "contracts" / "alertes" / "catalogue.yaml"
MAINTENANT = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
UNE_HEURE = timedelta(hours=1)
ACCEPTEE = StatutLivraison.ACCEPTEE.value
AVEC_REJETS = StatutLivraison.ACCEPTEE_AVEC_REJETS.value
BLOQUEE = StatutLivraison.BLOQUEE.value
REFUSEE = StatutLivraison.REFUSEE.value


def _livraison(origine, arrivee, statut=ACCEPTEE):
    """Une ligne de registre : seuls l'origine, l'arrivée et le statut comptent pour la surveillance."""
    pays, usine, ligne = origine if origine else (None, None, None)
    return LigneRegistre(
        livraison_id=f"livraison_{pays}_{usine}_{ligne}_{arrivee:%H%M%S}.csv", statut=statut, version_contrat="2.0.0",
        nb_recues=100, nb_valides=100, nb_rejetees=0, codes_refus=(), pays=pays, usine=usine, ligne=ligne,
        horodatage_fichier=None if origine is None else arrivee - timedelta(minutes=10), heure_arrivee=arrivee,
        empreinte=None, rejeu_de=None,
    )


def _source(origine=("FRA", "AA", "001"), heures=24):
    pays, usine, ligne = origine
    return surveillance.SourceAttendue(pays=pays, usine=usine, ligne=ligne, delai_max=timedelta(hours=heures))


FRA = ("FRA", "AA", "001")
DEU = ("DEU", "BA", "001")
USA = ("USA", "CA", "001")


def _constats(registre, sources, maintenant=MAINTENANT):
    return surveillance.controler_fraicheur(registre, sources, maintenant)


# ==============================================================================
# 1. 📜 LE FICHIER DES SOURCES ATTENDUES : un chargeur qui refuse tout ce qui est ambigu
# ==============================================================================
def _ecrire(tmp_path, texte):
    chemin = tmp_path / "sources.yaml"
    chemin.write_text(texte, encoding="utf-8")
    return chemin


BON = 'version: "1.0.0"\nsources:\n  - pays: FRA\n    usine: AA\n    ligne: "001"\n    delai_max_heures: 24\n'


def test_le_vrai_fichier_des_sources_attendues_se_charge():
    sources = surveillance.charger_sources_attendues(CHEMIN_SOURCES)

    assert len(sources) >= 3
    assert all(isinstance(source, surveillance.SourceAttendue) and source.delai_max > timedelta(0) for source in sources)
    assert len({(s.pays, s.usine, s.ligne) for s in sources}) == len(sources)


def test_un_fichier_valide_donne_ses_sources_avec_le_delai_en_duree(tmp_path):
    texte = BON + '  - pays: DEU\n    usine: BA\n    ligne: "002"\n    delai_max_heures: 0.5\n'

    sources = surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))

    assert sources == (surveillance.SourceAttendue("DEU", "BA", "002", timedelta(minutes=30)), _source(FRA, 24))


def test_les_sources_sont_rendues_dans_l_ordre_pays_usine_ligne(tmp_path):
    texte = BON + '  - pays: DEU\n    usine: BA\n    ligne: "001"\n    delai_max_heures: 24\n'

    sources = surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))

    assert [(s.pays, s.usine, s.ligne) for s in sources] == [DEU, FRA]


def test_un_fichier_absent_leve_une_erreur_de_fichier_pas_une_erreur_de_contenu(tmp_path):
    with pytest.raises(FileNotFoundError):
        surveillance.charger_sources_attendues(tmp_path / "absent.yaml")


@pytest.mark.parametrize("texte", ["", "[1, 2]", "sources: 3\n", "version: '1'\n", "sources: []\n", "sources:\n  - 4\n", "a: [\n"],
                         ids=["vide", "liste", "sources pas une liste", "sans sources", "sources vides", "pas un dict", "yaml cassé"])
def test_un_fichier_mal_forme_est_refuse(tmp_path, texte):
    with pytest.raises(surveillance.SourcesInvalides):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_les_erreurs_de_contenu_sont_des_valueerror(tmp_path):
    assert issubclass(surveillance.SourcesInvalides, ValueError)
    with pytest.raises(ValueError):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, "sources: []\n"))


@pytest.mark.parametrize("champ", ["pays", "usine", "ligne", "delai_max_heures"])
def test_un_champ_manquant_est_refuse_avec_son_nom(tmp_path, champ):
    source = {"pays": "FRA", "usine": "AA", "ligne": "001", "delai_max_heures": 24}
    del source[champ]
    champs = [f"{cle}: {valeur!r}" for cle, valeur in source.items()]
    lignes = ["sources:", f"  - {champs[0]}", *(f"    {champ}" for champ in champs[1:])]

    with pytest.raises(surveillance.SourcesInvalides, match=champ):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, "\n".join(lignes) + "\n"))


AUTRE_SOURCE = 'sources:\n  - pays: DEU\n    usine: BA\n    ligne: "001"\n    delai_max_heures: 24\n'


@pytest.mark.parametrize(
    ("texte", "cle"),
    [
        (BON + "    delai_max_heures: 9999\n", "delai_max_heures"),
        (BON + '    pays: DEU\n', "pays"),
        (BON + AUTRE_SOURCE, "sources"),
        ('version: "1.0.0"\n' + BON, "version"),
        (BON + '  - pays: DEU\n    usine: BA\n    ligne: "001"\n    delai_max_heures: 24\n    usine: BB\n', "usine"),
    ],
    ids=["délai répété", "pays répété", "sources répété", "version répétée", "clé répétée dans la seconde source"],
)
def test_une_cle_repetee_est_refusee_avec_son_nom_au_lieu_de_laisser_la_derniere_gagner(tmp_path, texte, cle):
    """PyYAML garde en silence la dernière valeur d'une clé répétée : une copie-colle ferait disparaître des sources de la surveillance."""
    nom_de_la_cle = f"clé « {cle} » est répétée"                    # le nom de la clé, pas seulement l'extrait de ligne
    with pytest.raises(surveillance.SourcesInvalides, match=nom_de_la_cle):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_la_meme_cle_dans_deux_sources_differentes_n_est_pas_une_repetition(tmp_path):
    texte = BON + '  - pays: DEU\n    usine: BA\n    ligne: "001"\n    delai_max_heures: 24\n'

    assert len(surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))) == 2


def test_une_fusion_yaml_n_est_pas_prise_pour_une_cle_repetee(tmp_path):
    """`<<: *base` reprend les champs d'une source puis en remplace certains : c'est du YAML légitime, pas une répétition."""
    texte = (
        'sources:\n  - &base\n    pays: FRA\n    usine: AA\n    ligne: "001"\n    delai_max_heures: 24\n'
        '  - <<: *base\n    ligne: "002"\n'
    )

    sources = surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))

    assert [(s.pays, s.usine, s.ligne) for s in sources] == [("FRA", "AA", "001"), ("FRA", "AA", "002")]


def test_une_cle_qui_n_est_pas_un_texte_simple_est_refusee_sans_planter(tmp_path):
    """Une clé liste est impossible à comparer comme les autres : le refus doit rester une erreur de contenu, pas un TypeError."""
    with pytest.raises(surveillance.SourcesInvalides):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, "? [a, b]\n: 1\nsources: []\n"))


def test_une_cle_inconnue_au_sommet_du_fichier_est_refusee_avec_son_nom(tmp_path):
    with pytest.raises(surveillance.SourcesInvalides, match="source_attendues"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, BON + "source_attendues: []\n"))


def test_un_delai_trop_grand_pour_une_duree_est_refuse_proprement(tmp_path):
    texte = BON.replace("delai_max_heures: 24", "delai_max_heures: 1.0e+30")

    with pytest.raises(surveillance.SourcesInvalides, match="delai_max_heures"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_un_delai_de_zero_dit_qu_il_doit_etre_positif_et_pas_qu_il_est_trop_petit(tmp_path):
    texte = BON.replace("delai_max_heures: 24", "delai_max_heures: 0")

    with pytest.raises(surveillance.SourcesInvalides, match="fini et positif"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


@pytest.mark.parametrize("valeur", ["1.0e-12", "1.0e-320", "1.0e-300"], ids=["un picoheure", "plus petit que tout", "minuscule"])
def test_un_delai_positif_qui_devient_une_duree_nulle_est_refuse(tmp_path, valeur):
    """Le nombre est positif mais la durée arrondie à la microseconde vaut zéro : toute livraison serait immédiatement en retard."""
    texte = BON.replace("delai_max_heures: 24", f"delai_max_heures: {valeur}")

    with pytest.raises(surveillance.SourcesInvalides, match="delai_max_heures"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_un_tout_petit_delai_qui_reste_une_vraie_duree_est_accepte(tmp_path):
    texte = BON.replace("delai_max_heures: 24", "delai_max_heures: 0.001")

    (source,) = surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))

    assert source.delai_max == timedelta(seconds=3.6)


def test_un_champ_inconnu_est_refuse_avec_son_nom_car_c_est_peut_etre_une_faute_de_frappe(tmp_path):
    with pytest.raises(surveillance.SourcesInvalides, match="delai_max_heure"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, BON + "    delai_max_heure: 3\n"))


@pytest.mark.parametrize(
    ("champ", "valeur"),
    [("pays", "fra"), ("pays", "FR"), ("pays", "FRAN"), ("pays", "F1A"), ("usine", "A"), ("usine", "aa"), ("usine", "AAA"),
     ("ligne", '"01"'), ("ligne", '"0001"'), ("ligne", '"00A"'), ("ligne", '"١٢٣"')],
)
def test_pays_usine_et_ligne_suivent_la_convention_du_nom_de_fichier(tmp_path, champ, valeur):
    texte = re.sub(rf"({champ}: ).*", rf"\g<1>{valeur}", BON)

    with pytest.raises(surveillance.SourcesInvalides, match=champ):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


@pytest.mark.parametrize("champ", ["pays", "usine", "ligne"])
@pytest.mark.parametrize("valeur", ["[FRA]", "null", "true", "1.5", "{a: 1}"], ids=["liste", "vide", "booléen", "décimal", "dictionnaire"])
def test_un_champ_d_origine_qui_n_est_pas_du_texte_est_refuse_avec_une_erreur_de_contenu(tmp_path, champ, valeur):
    texte = re.sub(rf"({champ}: ).*", rf"\g<1>{valeur}", BON)

    with pytest.raises(surveillance.SourcesInvalides, match=champ):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_une_ligne_ecrite_sans_guillemets_est_refusee_avec_le_conseil_des_guillemets(tmp_path):
    """YAML lit 001 comme l'entier 1 : sans l'explication, l'erreur serait incompréhensible."""
    texte = BON.replace('"001"', "001")

    with pytest.raises(surveillance.SourcesInvalides, match="guillemets"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


@pytest.mark.parametrize("valeur", ["0", "-1", "-0.5", ".nan", ".inf", "-.inf", '"24"', "true", "false", "null", "[24]", "[]"],
                         ids=["zéro", "négatif", "négatif décimal", "nan", "inf", "-inf", "texte", "vrai", "faux", "vide", "liste", "[]"])
def test_un_delai_qui_n_est_pas_un_nombre_fini_et_positif_est_refuse(tmp_path, valeur):
    texte = BON.replace("delai_max_heures: 24", f"delai_max_heures: {valeur}")

    with pytest.raises(surveillance.SourcesInvalides, match="delai_max_heures"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_deux_fois_la_meme_source_est_refusee_avec_son_origine(tmp_path):
    with pytest.raises(surveillance.SourcesInvalides, match="FRA_AA_001"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, BON + BON.split("sources:\n")[1]))


def test_l_erreur_dit_quelle_source_est_en_cause_par_son_rang(tmp_path):
    texte = BON + '  - pays: DEU\n    usine: BA\n    ligne: "002"\n    delai_max_heures: -3\n'

    with pytest.raises(surveillance.SourcesInvalides, match="source 2"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, texte))


def test_le_vrai_fichier_ne_contient_ni_adresse_ni_secret_ni_nom_de_personne():
    texte = CHEMIN_SOURCES.read_text(encoding="utf-8")
    donnees = "\n".join(ligne.split("#")[0] for ligne in texte.splitlines())          # les commentaires ne sont pas des données

    assert "://" not in texte and "@" not in donnees
    cles = set(re.findall(r"^\s*(?:- )?([a-z_]+):", donnees, re.MULTILINE))
    assert cles <= {"version", "sources", "pays", "usine", "ligne", "delai_max_heures"}


# ==============================================================================
# 2. ⏱️ LA FRAÎCHEUR : comptée à la main
# ==============================================================================
def test_les_codes_de_constat_sont_ceux_annonces():
    assert surveillance.AUCUNE_LIVRAISON_INTEGREE == "AUCUNE_LIVRAISON_INTEGREE"
    assert surveillance.LIVRAISON_EN_RETARD == "LIVRAISON_EN_RETARD"
    assert surveillance.SOURCE_INCONNUE == "SOURCE_INCONNUE"


def test_une_source_qui_a_livre_dans_le_delai_n_a_aucun_constat():
    assert _constats([_livraison(FRA, MAINTENANT - timedelta(hours=23))], [_source(FRA, 24)]) == []


def test_une_source_en_retard_a_un_constat_complet():
    arrivee = MAINTENANT - timedelta(hours=30)

    (constat,) = _constats([_livraison(FRA, arrivee)], [_source(FRA, 24)])

    assert constat == surveillance.Constat(
        code="LIVRAISON_EN_RETARD", pays="FRA", usine="AA", ligne="001", derniere_arrivee=arrivee, age=timedelta(hours=30),
        delai_max=timedelta(hours=24), dernier_statut=ACCEPTEE,
    )


def test_a_pile_le_delai_c_est_encore_frais_une_seconde_de_plus_c_est_un_retard():
    pile = [_livraison(FRA, MAINTENANT - timedelta(hours=24))]
    un_peu_plus = [_livraison(FRA, MAINTENANT - timedelta(hours=24, seconds=1))]

    assert _constats(pile, [_source(FRA, 24)]) == []
    assert [c.code for c in _constats(un_peu_plus, [_source(FRA, 24)])] == ["LIVRAISON_EN_RETARD"]


def test_un_delai_en_heures_decimales_est_compte_en_minutes():
    registre = [_livraison(FRA, MAINTENANT - timedelta(minutes=31))]

    assert [c.age for c in _constats(registre, [_source(FRA, 0.5)])] == [timedelta(minutes=31)]
    assert _constats([_livraison(FRA, MAINTENANT - timedelta(minutes=29))], [_source(FRA, 0.5)]) == []


def test_une_source_qui_n_a_jamais_livre_est_signalee_sans_age_ni_derniere_arrivee():
    (constat,) = _constats([], [_source(FRA, 24)])

    assert constat == surveillance.Constat(
        code="AUCUNE_LIVRAISON_INTEGREE", pays="FRA", usine="AA", ligne="001", derniere_arrivee=None, age=None,
        delai_max=timedelta(hours=24), dernier_statut=None,
    )


def test_c_est_la_derniere_livraison_integree_qui_compte_pas_la_premiere_ni_l_ordre_du_registre():
    anciennes = [_livraison(FRA, MAINTENANT - timedelta(hours=h)) for h in (50, 3, 70)]

    assert _constats(anciennes, [_source(FRA, 24)]) == []
    (constat,) = _constats(anciennes, [_source(FRA, 2)])
    assert constat.derniere_arrivee == MAINTENANT - timedelta(hours=3) and constat.age == timedelta(hours=3)


def test_a_arrivee_egale_c_est_la_derniere_livraison_du_registre_qui_donne_le_dernier_statut():
    arrivee = MAINTENANT - timedelta(hours=30)
    integree_puis_bloquee = [_livraison(FRA, arrivee, ACCEPTEE), _livraison(FRA, arrivee, BLOQUEE)]
    bloquee_puis_integree = [_livraison(FRA, arrivee, BLOQUEE), _livraison(FRA, arrivee, ACCEPTEE)]

    assert [c.dernier_statut for c in _constats(integree_puis_bloquee, [_source(FRA, 24)])] == [BLOQUEE]
    assert [c.dernier_statut for c in _constats(bloquee_puis_integree, [_source(FRA, 24)])] == [ACCEPTEE]


def test_la_source_attendue_est_immuable():
    with pytest.raises(AttributeError):
        _source(FRA, 24).usine = "ZZ"


def test_une_arrivee_dans_le_futur_n_est_pas_un_retard():
    """Une horloge décalée ne doit pas inventer d'alerte."""
    assert _constats([_livraison(FRA, MAINTENANT + UNE_HEURE)], [_source(FRA, 1)]) == []


@pytest.mark.parametrize("statut", [ACCEPTEE, AVEC_REJETS])
def test_une_livraison_integree_rafraichit_la_source(statut):
    assert _constats([_livraison(FRA, MAINTENANT - UNE_HEURE, statut)], [_source(FRA, 2)]) == []


@pytest.mark.parametrize("statut", [BLOQUEE, REFUSEE, STATUT_REJEU])
def test_une_livraison_bloquee_refusee_ou_rejouee_ne_rafraichit_pas_la_source(statut):
    (constat,) = _constats([_livraison(FRA, MAINTENANT - UNE_HEURE, statut)], [_source(FRA, 2)])

    assert constat.code == "AUCUNE_LIVRAISON_INTEGREE" and constat.dernier_statut == statut


def test_une_livraison_bloquee_apres_une_integree_ne_la_rafraichit_pas_mais_le_constat_dit_le_dernier_statut():
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=30)), _livraison(FRA, MAINTENANT - UNE_HEURE, BLOQUEE)]

    (constat,) = _constats(registre, [_source(FRA, 24)])

    assert constat.code == "LIVRAISON_EN_RETARD" and constat.age == timedelta(hours=30)
    assert constat.dernier_statut == BLOQUEE


def test_un_rejeu_recent_ne_rafraichit_pas_et_le_constat_le_dit():
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=30)), _livraison(FRA, MAINTENANT - UNE_HEURE, STATUT_REJEU)]

    (constat,) = _constats(registre, [_source(FRA, 24)])

    assert constat.age == timedelta(hours=30) and constat.dernier_statut == STATUT_REJEU


def test_chaque_source_est_jugee_avec_son_propre_delai():
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=5)), _livraison(DEU, MAINTENANT - timedelta(hours=5))]

    constats = _constats(registre, [_source(FRA, 4), _source(DEU, 6)])

    assert [(c.pays, c.code) for c in constats] == [("FRA", "LIVRAISON_EN_RETARD")]


def test_deux_sources_d_une_meme_usine_ne_se_rafraichissent_pas_l_une_l_autre():
    registre = [_livraison(("FRA", "AA", "001"), MAINTENANT - UNE_HEURE)]

    constats = _constats(registre, [_source(("FRA", "AA", "001"), 2), _source(("FRA", "AA", "002"), 2)])

    assert [(c.ligne, c.code) for c in constats] == [("002", "AUCUNE_LIVRAISON_INTEGREE")]


# ==============================================================================
# 3. ❓ LES SOURCES INCONNUES
# ==============================================================================
def test_une_origine_qui_livre_sans_etre_declaree_est_signalee_une_seule_fois():
    registre = [_livraison(USA, MAINTENANT - timedelta(hours=h)) for h in (5, 2, 9)]

    (constat,) = [c for c in _constats(registre, [_source(FRA, 24)]) if c.pays == "USA"]

    assert constat == surveillance.Constat(
        code="SOURCE_INCONNUE", pays="USA", usine="CA", ligne="001", derniere_arrivee=MAINTENANT - timedelta(hours=2),
        age=timedelta(hours=2), delai_max=None, dernier_statut=ACCEPTEE,
    )


def test_une_origine_inconnue_qui_livre_dans_le_futur_a_un_age_nul_et_pas_negatif():
    """Comme pour une source déclarée, une horloge décalée ne doit pas produire de valeur absurde."""
    arrivee = MAINTENANT + UNE_HEURE

    (constat,) = _constats([_livraison(USA, arrivee)], [])

    assert constat.code == "SOURCE_INCONNUE" and constat.age == timedelta(0)
    assert constat.derniere_arrivee == arrivee


def test_l_age_d_une_origine_inconnue_qui_vient_d_arriver_est_nul():
    (constat,) = _constats([_livraison(USA, MAINTENANT)], [])

    assert constat.age == timedelta(0)


def test_une_origine_inconnue_est_signalee_meme_si_toutes_ses_livraisons_ont_ete_refusees():
    registre = [_livraison(USA, MAINTENANT - UNE_HEURE, REFUSEE)]

    (constat,) = _constats(registre, [])

    assert constat.code == "SOURCE_INCONNUE" and constat.dernier_statut == REFUSEE


def test_un_nom_de_fichier_invalide_n_a_pas_d_origine_donc_ne_cree_aucun_constat():
    registre = [_livraison(None, MAINTENANT - UNE_HEURE, REFUSEE)]

    assert _constats(registre, [_source(FRA, 24)]) == [
        surveillance.Constat("AUCUNE_LIVRAISON_INTEGREE", "FRA", "AA", "001", None, None, timedelta(hours=24), None)
    ]


def test_une_source_declaree_n_est_jamais_inconnue():
    assert [c.code for c in _constats([_livraison(FRA, MAINTENANT)], [_source(FRA, 24)])] == []


# ==============================================================================
# 4. 🧱 LES CONSTATS : ordre, forme, entrées
# ==============================================================================
def test_une_source_declaree_deux_fois_a_la_fonction_est_refusee():
    with pytest.raises(ValueError, match="FRA_AA_001"):
        _constats([], [_source(FRA, 24), _source(FRA, 48)])


def test_les_constats_sont_tries_par_pays_usine_ligne_puis_code():
    registre = [_livraison(USA, MAINTENANT - UNE_HEURE), _livraison(("DEU", "BA", "009"), MAINTENANT - UNE_HEURE)]
    sources = [_source(FRA, 24), _source(("FRA", "AA", "000"), 24)]

    cles = [(c.pays, c.usine, c.ligne, c.code) for c in _constats(registre, sources)]

    assert cles == [
        ("DEU", "BA", "009", "SOURCE_INCONNUE"),
        ("FRA", "AA", "000", "AUCUNE_LIVRAISON_INTEGREE"),
        ("FRA", "AA", "001", "AUCUNE_LIVRAISON_INTEGREE"),
        ("USA", "CA", "001", "SOURCE_INCONNUE"),
    ]


def test_l_ordre_du_registre_et_des_sources_ne_change_rien():
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=h), s) for h, s in ((30, ACCEPTEE), (40, AVEC_REJETS), (1, BLOQUEE))]
    registre += [_livraison(USA, MAINTENANT - timedelta(hours=7))]
    sources = [_source(FRA, 24), _source(DEU, 24)]

    assert _constats(registre, sources) == _constats(list(reversed(registre)), list(reversed(sources)))


def test_le_constat_est_immuable():
    (constat,) = _constats([], [_source(FRA, 24)])

    with pytest.raises(AttributeError):
        constat.code = "AUTRE"


def test_la_fonction_ne_modifie_pas_ses_entrees():
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=30)), _livraison(USA, MAINTENANT - UNE_HEURE)]
    sources = [_source(FRA, 24)]
    avant = (list(registre), list(sources))

    _constats(registre, sources)

    assert (registre, sources) == avant


def test_la_fonction_accepte_n_importe_quel_iterable():
    registre = (_livraison(FRA, MAINTENANT - timedelta(hours=30)) for _ in range(1))

    assert [c.code for c in _constats(registre, iter([_source(FRA, 24)]))] == ["LIVRAISON_EN_RETARD"]


def test_un_registre_et_des_sources_vides_ne_donnent_rien():
    assert _constats([], []) == []


def test_un_maintenant_sans_fuseau_est_refuse_avec_une_erreur_claire():
    with pytest.raises(ValueError, match="fuseau"):
        _constats([], [_source(FRA, 24)], datetime(2026, 11, 14, 12, 0))


def test_un_maintenant_dans_un_autre_fuseau_donne_les_memes_ages():
    from datetime import timezone

    paris = timezone(timedelta(hours=1))
    registre = [_livraison(FRA, MAINTENANT - timedelta(hours=30))]

    assert _constats(registre, [_source(FRA, 24)], MAINTENANT.astimezone(paris)) == _constats(registre, [_source(FRA, 24)])


def test_une_arrivee_sans_fuseau_au_registre_est_refusee_avec_une_erreur_claire():
    naive = _livraison(FRA, datetime(2026, 11, 14, 1, 0))

    with pytest.raises(ValueError, match="fuseau"):
        _constats([naive], [_source(FRA, 24)])


# ==============================================================================
# 5. 📅 LA JOURNÉE MULTI-USINES : attendus recalculés à part depuis la liste des livraisons
# ==============================================================================
STATUTS_INTEGRES = (ACCEPTEE, AVEC_REJETS)


@pytest.fixture(scope="module")
def journee(contrat, tmp_path_factory):
    livraisons = journee_multi_usines(contrat)
    etat = Etat()
    jouer(contrat, charger_catalogue(CHEMIN_CATALOGUE), livraisons, tmp_path_factory.mktemp("jour-surveillance"), etat)
    return livraisons, etat


def _origine(livraison):
    try:
        nom = lire_nom_fichier(livraison.nom)
    except Exception:                      # le nom invalide de la journée : pas d'origine
        return None
    return (nom.pays, nom.usine, nom.ligne)


def _attendu(livraisons):
    """Pour chaque origine : l'arrivée de sa dernière livraison intégrée (ou None), et toutes ses origines, d'après les RECETTES."""
    integrees, toutes = {}, set()
    for livraison in livraisons:
        origine = _origine(livraison)
        if origine is None:
            continue
        toutes.add(origine)
        if livraison.attendu.statut in STATUTS_INTEGRES:
            integrees[origine] = max(livraison.arrivee, integrees.get(origine, livraison.arrivee))
    return integrees, toutes


def test_la_journee_a_de_quoi_etre_surveillee(journee):
    livraisons, etat = journee
    integrees, toutes = _attendu(livraisons)

    assert len(integrees) >= 6 and len(toutes) - len(integrees) >= 4          # des origines intégrées, et d'autres qui n'ont jamais intégré
    assert len(etat.registre) == len(livraisons)


def test_en_declarant_les_sources_integrees_seules_les_autres_origines_sont_inconnues(journee):
    livraisons, etat = journee
    integrees, toutes = _attendu(livraisons)
    derniere = max(livraison.arrivee for livraison in livraisons)
    sources = [_source(origine, 24) for origine in integrees]          # arrivées étalées sur 4 heures : 24 h les laisse fraîches

    constats = _constats(etat.registre, sources, derniere + UNE_HEURE)

    assert {(c.pays, c.usine, c.ligne) for c in constats} == toutes - set(integrees)
    assert {c.code for c in constats} == {"SOURCE_INCONNUE"}


def test_en_declarant_toutes_les_origines_celles_qui_n_ont_jamais_integre_sont_signalees_avec_leur_statut(journee):
    livraisons, etat = journee
    integrees, toutes = _attendu(livraisons)
    derniere = max(livraison.arrivee for livraison in livraisons)

    constats = _constats(etat.registre, [_source(origine, 24) for origine in toutes], derniere + UNE_HEURE)

    assert {(c.pays, c.usine, c.ligne) for c in constats} == toutes - set(integrees)
    assert {c.code for c in constats} == {"AUCUNE_LIVRAISON_INTEGREE"}
    assert {c.dernier_statut for c in constats} == {REFUSEE}


def test_a_mi_journee_exactement_les_sources_dont_la_derniere_integration_est_trop_ancienne_sont_en_retard(journee):
    livraisons, etat = journee
    integrees, toutes = _attendu(livraisons)
    maintenant = PREMIERE_ARRIVEE + timedelta(hours=2, minutes=30)
    delai = timedelta(hours=1)

    constats = _constats(etat.registre, [_source(origine, 1) for origine in integrees], maintenant)

    en_retard = {origine for origine, arrivee in integrees.items() if maintenant - arrivee > delai}
    assert 0 < len(en_retard) < len(integrees)                      # un vrai mélange de frais et de retards
    assert {(c.pays, c.usine, c.ligne) for c in constats if c.code == "LIVRAISON_EN_RETARD"} == en_retard
    for constat in constats:
        if constat.code == "LIVRAISON_EN_RETARD":
            assert constat.derniere_arrivee == integrees[(constat.pays, constat.usine, constat.ligne)]
            assert constat.age == maintenant - constat.derniere_arrivee


def test_une_source_declaree_en_plus_n_a_jamais_livre(journee):
    livraisons, etat = journee
    integrees, _ = _attendu(livraisons)
    derniere = max(livraison.arrivee for livraison in livraisons)
    sources = [_source(origine, 24) for origine in integrees] + [_source(("ESP", "ZZ", "999"), 24)]

    constats = _constats(etat.registre, sources, derniere + UNE_HEURE)

    assert [(c.pays, c.code, c.dernier_statut) for c in constats if c.pays == "ESP"] == [("ESP", "AUCUNE_LIVRAISON_INTEGREE", None)]


def test_une_source_retiree_de_la_declaration_devient_inconnue(journee):
    livraisons, etat = journee
    integrees, toutes = _attendu(livraisons)
    derniere = max(livraison.arrivee for livraison in livraisons)
    retiree = FRA
    sources = [_source(origine, 24) for origine in toutes if origine != retiree]

    constats = _constats(etat.registre, sources, derniere + UNE_HEURE)

    assert [c.code for c in constats if (c.pays, c.usine, c.ligne) == retiree] == ["SOURCE_INCONNUE"]


def test_rejouer_la_journee_le_lendemain_ne_rafraichit_aucune_source(contrat, tmp_path):
    """Les rejeux arrivent plus tard mais ne rafraîchissent rien : les retards se comptent depuis la première intégration."""
    catalogue = charger_catalogue(CHEMIN_CATALOGUE)
    livraisons = journee_multi_usines(contrat)
    etat = Etat()
    jouer(contrat, catalogue, livraisons, tmp_path / "jour", etat)
    integrees, _ = _attendu(livraisons)
    maintenant = max(livraison.arrivee for livraison in livraisons) + timedelta(hours=40)
    sources = [_source(origine, 24) for origine in integrees]

    def en_retard():
        constats = _constats(etat.registre, sources, maintenant)
        return {(c.pays, c.usine, c.ligne): c.derniere_arrivee for c in constats if c.code == "LIVRAISON_EN_RETARD"}

    avant = en_retard()
    jouer(contrat, catalogue, livraisons, tmp_path / "lendemain", etat, decalage=timedelta(hours=20))

    assert avant == integrees                         # toutes en retard, depuis leur vraie dernière intégration
    assert en_retard() == avant                       # le rejeu, arrivé 20 heures plus tard, n'a rien rafraîchi


# ==============================================================================
# 6. 🔒 LA SURVEILLANCE NE TOUCHE À RIEN
# ==============================================================================
def test_la_surveillance_n_ecrit_aucun_fichier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    _constats([_livraison(FRA, MAINTENANT - timedelta(hours=30))], [_source(FRA, 24)])

    assert list(tmp_path.iterdir()) == []


def test_importer_le_module_n_importe_ni_spark_ni_le_sdk():
    code = "import sys, rd_manuf.surveillance; sys.exit(1 if any(m.split('.')[0] in ('databricks', 'pyspark') for m in sys.modules) else 0)"

    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
