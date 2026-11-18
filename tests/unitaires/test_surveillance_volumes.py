"""La SURVEILLANCE, troisième axe : les VOLUMES de livraison, par source.

Une usine peut livrer à l'heure, sans un seul rejet, et pourtant livrer la moitié de ses lignes (un export coupé en route) ou le double (un
fichier envoyé deux fois, une plage de lots dupliquée) : ni la fraîcheur ni le taux de rejet ne le voient. La fonction pure
`controler_volumes` compare le volume (`nb_recues`) de la DERNIÈRE livraison de chaque source à la MÉDIANE de ses `fenetre_livraisons`
livraisons précédentes, et signale l'écart relatif qui dépasse (strictement) `ecart_max`, en baisse comme en hausse.

Les valeurs attendues sont comptées ICI, à la main ; la journée multi-usines de la #41 sert de preuve d'ensemble. Comme les autres axes,
la fonction ne lit que le registre qu'on lui donne, n'écrit rien et ne prévient personne.

Le module est importé par son nom (`surveillance.controler_volumes`) pour qu'un code absent fasse échouer chaque test sur son assertion,
et non la collecte de tout le fichier.
"""
import dataclasses
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rd_manuf import surveillance
from rd_manuf.alertes import charger_catalogue
from rd_manuf.contrat import StatutLivraison
from rd_manuf.journee import Etat, jouer, journee_multi_usines
from rd_manuf.registre import STATUT_REJEU, LigneRegistre

RACINE = Path(__file__).resolve().parents[2]
CHEMIN_SOURCES = RACINE / "contracts" / "surveillance" / "sources_attendues.yaml"
CHEMIN_CATALOGUE = RACINE / "contracts" / "alertes" / "catalogue.yaml"
MAINTENANT = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
ACCEPTEE = StatutLivraison.ACCEPTEE.value
AVEC_REJETS = StatutLivraison.ACCEPTEE_AVEC_REJETS.value
BLOQUEE = StatutLivraison.BLOQUEE.value
REFUSEE = StatutLivraison.REFUSEE.value
FRA = ("FRA", "AA", "001")
DEU = ("DEU", "BA", "001")
USA = ("USA", "CA", "001")
_RANG = itertools.count()


def _livraison(volume, heures, statut=ACCEPTEE, origine=FRA):
    """Une ligne de registre : seuls l'origine, le statut, l'arrivée (en heures avant « maintenant ») et le volume comptent."""
    pays, usine, ligne = origine if origine else (None, None, None)
    return LigneRegistre(
        livraison_id=f"livraison_{pays}_{usine}_{ligne}_{next(_RANG)}.csv", statut=statut, version_contrat="2.0.0", nb_recues=volume,
        nb_valides=volume, nb_rejetees=0, codes_refus=(), pays=pays, usine=usine, ligne=ligne, horodatage_fichier=None,
        heure_arrivee=MAINTENANT - timedelta(hours=heures), empreinte=None, rejeu_de=None,
    )


def _historique(volumes, origine=FRA, statut=ACCEPTEE):
    """Une livraison par volume, de la plus ancienne à la plus récente, à une heure d'écart ; la dernière : 1 h avant « maintenant »."""
    return [_livraison(volume, len(volumes) - rang, statut, origine) for rang, volume in enumerate(volumes)]


def _parametres_de(fenetre=3, ecart=0.5):
    return surveillance.ParametresVolumes(fenetre_livraisons=fenetre, ecart_max=ecart)


def _volumes(registre, parametres=None):
    return surveillance.controler_volumes(registre, parametres or _parametres_de())


# ==============================================================================
# 1. 📜 LES PARAMÈTRES, lus dans le fichier de surveillance (section « volumes »)
# ==============================================================================
BON = 'version: "1.2.0"\nsources:\n  - pays: FRA\n    usine: AA\n    ligne: "001"\n    delai_max_heures: 24\n'
VOLUMES = "volumes:\n  fenetre_livraisons: 5\n  ecart_max: 0.5\n"


def _ecrire(tmp_path, texte):
    chemin = tmp_path / "sources.yaml"
    chemin.write_text(texte, encoding="utf-8")
    return chemin


def _parametres(tmp_path, texte):
    return surveillance.charger_parametres_volumes(_ecrire(tmp_path, texte))


def test_le_vrai_fichier_donne_des_parametres_de_volumes_utilisables():
    parametres = surveillance.charger_parametres_volumes(CHEMIN_SOURCES)

    assert isinstance(parametres, surveillance.ParametresVolumes)
    assert parametres.fenetre_livraisons >= 1 and parametres.ecart_max > 0


def test_des_parametres_valides_se_chargent(tmp_path):
    assert _parametres(tmp_path, BON + VOLUMES) == surveillance.ParametresVolumes(fenetre_livraisons=5, ecart_max=0.5)


def test_un_ecart_entier_ou_superieur_a_un_est_accepte_et_rendu_en_decimal(tmp_path):
    """Une hausse n'a pas de plafond : un volume qui triple est un écart de 2."""
    parametres = _parametres(tmp_path, BON + VOLUMES.replace("0.5", "2"))

    assert parametres.ecart_max == 2.0 and isinstance(parametres.ecart_max, float)


def test_le_chargeur_des_sources_accepte_un_fichier_avec_la_section_et_sans_elle(tmp_path):
    assert len(surveillance.charger_sources_attendues(_ecrire(tmp_path, BON + VOLUMES))) == 1
    assert len(surveillance.charger_sources_attendues(_ecrire(tmp_path, BON))) == 1


def test_les_parametres_sont_immuables(tmp_path):
    parametres = _parametres(tmp_path, BON + VOLUMES)

    with pytest.raises(AttributeError):
        parametres.ecart_max = 9


SECTIONS_INVALIDES = ["volumes:\n  fenetre_livraisons: 0\n  ecart_max: 9\n", "volumes: 5\n", "volumes:\n"]


@pytest.mark.parametrize("section", SECTIONS_INVALIDES, ids=["valeurs invalides", "pas un dictionnaire", "section vide"])
def test_le_chargeur_des_sources_refuse_aussi_une_section_presente_mais_invalide(tmp_path, section):
    """Un fichier doit avoir une seule validité : sans cela, le défaut n'apparaîtrait qu'à l'appel de l'autre chargeur.
    Le refus doit venir de la section elle-même, pas d'une « clé inconnue » : `volumes` doit d'abord être une clé permise."""
    with pytest.raises(surveillance.SourcesInvalides, match=r"\A(?!Clé inconnue).*volumes"):
        surveillance.charger_sources_attendues(_ecrire(tmp_path, BON + section))


@pytest.mark.parametrize("charger", ["charger_sources_attendues", "charger_parametres_volumes"])
def test_un_yaml_casse_parle_du_fichier_de_surveillance(tmp_path, charger):
    with pytest.raises(surveillance.SourcesInvalides, match="fichier de surveillance"):
        getattr(surveillance, charger)(_ecrire(tmp_path, BON + "volumes: [\n"))


def test_une_section_absente_est_refusee_avec_son_nom(tmp_path):
    with pytest.raises(surveillance.SourcesInvalides, match="volumes"):
        _parametres(tmp_path, BON)


@pytest.mark.parametrize("section", ["volumes: 5\n", "volumes: []\n", "volumes:\n", "volumes: {}\n"],
                         ids=["nombre", "liste", "vide", "dictionnaire vide"])
def test_une_section_mal_formee_est_refusee_avec_son_nom(tmp_path, section):
    with pytest.raises(surveillance.SourcesInvalides, match="volumes"):
        _parametres(tmp_path, BON + section)


@pytest.mark.parametrize("champ", ["fenetre_livraisons", "ecart_max"])
def test_un_champ_manquant_de_la_section_est_refuse_avec_son_nom(tmp_path, champ):
    lignes = [ligne for ligne in VOLUMES.splitlines() if champ not in ligne]

    with pytest.raises(surveillance.SourcesInvalides, match=champ):
        _parametres(tmp_path, BON + "\n".join(lignes) + "\n")


def test_un_champ_inconnu_de_la_section_est_refuse_avec_son_nom(tmp_path):
    with pytest.raises(surveillance.SourcesInvalides, match="ecart_maximum"):
        _parametres(tmp_path, BON + VOLUMES + "  ecart_maximum: 3\n")


@pytest.mark.parametrize("cle", ["fenetre_livraisons", "ecart_max"])
def test_une_cle_repetee_dans_la_section_est_refusee_avec_son_nom(tmp_path, cle):
    ligne = next(ligne for ligne in VOLUMES.splitlines() if cle in ligne)

    with pytest.raises(surveillance.SourcesInvalides, match=f"clé « {cle} » est répétée"):
        _parametres(tmp_path, BON + VOLUMES + ligne + "\n")


@pytest.mark.parametrize("valeur", ["0", "-1", "3.5", "3.0", "'5'", "true", "null", "[5]"],
                         ids=["zéro", "négatif", "décimal", "décimal rond", "texte", "booléen", "vide", "liste"])
def test_une_fenetre_qui_n_est_pas_un_entier_d_au_moins_un_est_refusee(tmp_path, valeur):
    with pytest.raises(surveillance.SourcesInvalides, match="fenetre_livraisons"):
        _parametres(tmp_path, BON + VOLUMES.replace("fenetre_livraisons: 5", f"fenetre_livraisons: {valeur}"))


def test_une_fenetre_d_une_seule_livraison_est_acceptee(tmp_path):
    assert _parametres(tmp_path, BON + VOLUMES.replace("5", "1")).fenetre_livraisons == 1


@pytest.mark.parametrize("valeur", ["0", "-0.1", ".nan", ".inf", "-.inf", "'0.5'", "true", "null", "[0.5]"],
                         ids=["zéro", "négatif", "nan", "inf", "-inf", "texte", "booléen", "vide", "liste"])
def test_un_ecart_qui_n_est_pas_un_nombre_fini_et_strictement_positif_est_refuse(tmp_path, valeur):
    with pytest.raises(surveillance.SourcesInvalides, match="ecart_max"):
        _parametres(tmp_path, BON + VOLUMES.replace("ecart_max: 0.5", f"ecart_max: {valeur}"))


@pytest.mark.parametrize(
    ("fenetre", "ecart", "champ"),
    [(0, 0.5, "fenetre_livraisons"), (-1, 0.5, "fenetre_livraisons"), (2.5, 0.5, "fenetre_livraisons"), (True, 0.5, "fenetre_livraisons"),
     (3, 0, "ecart_max"), (3, -0.1, "ecart_max"), (3, float("nan"), "ecart_max"), (3, float("inf"), "ecart_max"), (3, False, "ecart_max")],
    ids=["fenêtre 0", "fenêtre < 0", "fenêtre décimale", "fenêtre booléenne", "écart 0", "écart < 0", "écart nan", "écart inf",
         "écart booléen"],
)
def test_des_parametres_construits_a_la_main_sont_controles_comme_ceux_du_fichier(fenetre, ecart, champ):
    """Une fenêtre de zéro prendrait tout le registre (`liste[-0:]`) : le refus doit venir de l'objet, pas seulement du chargeur."""
    with pytest.raises(ValueError, match=champ):
        surveillance.ParametresVolumes(fenetre_livraisons=fenetre, ecart_max=ecart)


# ==============================================================================
# 2. 📊 LA RÈGLE, comptée à la main (fenêtre de 3 livraisons, écart maximal de 0,5 sauf mention)
# ==============================================================================
def test_le_code_du_constat_de_volume_est_celui_annonce():
    assert surveillance.VOLUME_ANORMAL == "VOLUME_ANORMAL"


def test_une_source_dont_la_derniere_livraison_est_dans_son_habitude_n_a_aucun_constat():
    assert _volumes(_historique([100, 110, 90, 105])) == []


def test_une_baisse_au_dela_de_l_ecart_maximal_est_un_constat_complet():
    constats = _volumes(_historique([100, 100, 100, 40]))

    assert len(constats) == 1
    constat = constats[0]
    assert (constat.code, (constat.pays, constat.usine, constat.ligne)) == ("VOLUME_ANORMAL", FRA)
    assert (constat.volume, constat.reference, constat.sens) == (40, 100.0, "baisse")
    assert constat.ecart == pytest.approx(-0.6) and constat.ecart_max == 0.5 and constat.nb_references == 3


def test_une_hausse_au_dela_de_l_ecart_maximal_est_un_constat_complet():
    constats = _volumes(_historique([100, 100, 100, 160]))

    assert [(c.volume, c.reference, c.sens) for c in constats] == [(160, 100.0, "hausse")]
    assert constats[0].ecart == pytest.approx(0.6)


def test_le_constat_garde_l_identifiant_de_la_derniere_livraison():
    registre = _historique([100, 100, 100, 40])

    assert _volumes(registre)[0].livraison_id == registre[-1].livraison_id


@pytest.mark.parametrize(
    ("volume", "sens"), [(150, None), (151, "hausse"), (50, None), (49, "baisse")],
    ids=["pile à +50 %", "juste au-dessus", "pile à -50 %", "juste en dessous"],
)
def test_a_pile_l_ecart_maximal_c_est_normal_un_de_plus_c_est_un_constat(volume, sens):
    constats = _volumes(_historique([100, 100, 100, volume]))

    assert [c.sens for c in constats] == ([sens] if sens else [])


def test_un_ecart_pile_au_seuil_n_est_pas_signale_a_cause_d_un_arrondi_de_flottants():
    """153 pour une référence de 90 est un écart de 0,7 exactement ; `0,7 × 90` vaut 63.00000000000001 en flottants."""
    registre = _historique([90, 90, 90, 153])

    assert _volumes(registre, _parametres_de(3, 0.7)) == []
    assert [c.sens for c in _volumes(_historique([90, 90, 90, 154]), _parametres_de(3, 0.7))] == ["hausse"]


def test_une_livraison_passee_aberrante_ne_deregle_pas_la_reference_car_c_est_la_mediane_qui_compte():
    """La moyenne de [100, 100, 1000] est 400 : une livraison de 100 serait une baisse de 75 %. La médiane est 100."""
    assert _volumes(_historique([100, 100, 1000, 100])) == []


def test_avec_une_fenetre_paire_la_reference_est_la_moyenne_des_deux_valeurs_du_milieu():
    constats = _volumes(_historique([100, 200, 250]), _parametres_de(2, 0.5))

    assert [(c.reference, c.sens) for c in constats] == [(150.0, "hausse")]
    assert _volumes(_historique([100, 200, 150]), _parametres_de(2, 0.5)) == []


def test_seules_les_livraisons_de_la_fenetre_font_la_reference_pas_tout_l_historique():
    constats = _volumes(_historique([1000, 100, 100, 100, 40]))

    assert [(c.reference, c.nb_references) for c in constats] == [(100.0, 3)]


def test_une_source_qui_a_moins_de_livraisons_que_la_fenetre_plus_une_n_est_pas_jugee():
    assert _volumes(_historique([100, 100, 40])) == []                                        # deux références pour une fenêtre de trois
    assert len(_volumes(_historique([100, 100, 100, 40]))) == 1                              # trois références : jugée


def test_la_derniere_livraison_est_celle_qui_est_arrivee_en_dernier_pas_la_derniere_du_registre():
    registre = _historique([100, 100, 100, 40])

    assert [c.volume for c in _volumes(list(reversed(registre)))] == [40]


def test_a_arrivee_egale_c_est_la_derniere_ligne_du_registre_qui_est_la_derniere_livraison():
    anciennes = _historique([100, 100, 100])
    meme_heure = [_livraison(100, 0), _livraison(40, 0)]

    assert [c.volume for c in _volumes(anciennes + meme_heure)] == [40]
    assert _volumes(anciennes + meme_heure[::-1]) == []


@pytest.mark.parametrize("statut", [REFUSEE, STATUT_REJEU], ids=["refusée", "rejouée"])
def test_une_livraison_refusee_ou_rejouee_n_est_jamais_la_derniere_livraison_meme_si_le_registre_a_garde_un_nombre(statut):
    """Ses lignes n'ont pas été lues, ou l'ont déjà été : comptée comme dernière, ce 5 serait une baisse de 95 %."""
    assert _volumes(_historique([100, 100, 100, 100]) + [_livraison(5, 0, statut)]) == []


@pytest.mark.parametrize("statut", [REFUSEE, STATUT_REJEU], ids=["refusée", "rejouée"])
def test_une_livraison_refusee_ou_rejouee_n_entre_pas_dans_la_reference(statut):
    """Comptées, ces deux 5 feraient une médiane de 5 et la dernière livraison (40) passerait pour une hausse."""
    registre = [_livraison(100, 6), _livraison(100, 5), _livraison(100, 4), _livraison(5, 3, statut), _livraison(5, 2, statut),
                _livraison(40, 0)]

    assert [(c.reference, c.sens) for c in _volumes(registre)] == [(100.0, "baisse")]


def test_une_livraison_sans_ligne_recue_ne_compte_pas():
    registre = _historique([100, 100, 100, 100]) + [_livraison(0, 0)]

    assert _volumes(registre) == []


def test_une_livraison_bloquee_compte_comme_derniere_livraison_et_comme_reference():
    """Un blocage pour trop de rejets est aussi un volume : le fichier a été lu."""
    assert [c.sens for c in _volumes(_historique([100, 100, 100]) + [_livraison(40, 0, BLOQUEE)])] == ["baisse"]
    comme_reference = [_livraison(40, 3, BLOQUEE), _livraison(40, 2), _livraison(40, 1, AVEC_REJETS), _livraison(100, 0)]
    assert [(c.reference, c.nb_references) for c in _volumes(comme_reference)] == [(40.0, 3)]


def test_un_nom_de_fichier_invalide_n_a_pas_d_origine_donc_ne_cree_aucun_constat():
    assert _volumes(_historique([100, 100, 100, 40], origine=None)) == []


def test_chaque_source_est_jugee_seule_et_les_constats_sont_tries_par_origine():
    registre = (_historique([100, 100, 100, 40], origine=USA) + _historique([100, 100, 100, 100], origine=DEU)
                + _historique([100, 100, 100, 200], origine=FRA))

    assert [((c.pays, c.usine, c.ligne), c.sens) for c in _volumes(registre)] == [(FRA, "hausse"), (USA, "baisse")]


def test_les_livraisons_d_une_autre_source_ne_comptent_pas_dans_la_reference():
    registre = _historique([100, 100, 100, 100], origine=FRA) + _historique([5, 5, 5], origine=DEU)

    assert _volumes(registre) == []


def test_le_constat_est_immuable():
    constat = _volumes(_historique([100, 100, 100, 40]))[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        constat.volume = 1


def test_la_fonction_ne_modifie_pas_ses_entrees_et_accepte_n_importe_quel_iterable():
    registre = _historique([100, 100, 100, 40])
    copie = list(registre)

    constats = surveillance.controler_volumes((ligne for ligne in registre), _parametres_de())

    assert registre == copie and [c.volume for c in constats] == [40]


def test_un_registre_vide_ne_donne_rien():
    assert _volumes([]) == []


def test_une_arrivee_sans_fuseau_est_refusee_avec_une_erreur_claire():
    sans_fuseau = dataclasses.replace(_livraison(100, 0), heure_arrivee=datetime(2026, 11, 14, 11, 0))

    with pytest.raises(ValueError, match="fuseau"):
        _volumes(_historique([100, 100, 100]) + [sans_fuseau])


# ==============================================================================
# 3. 🏭 LA JOURNÉE DE TEST : preuve d'ensemble, avec des attendus recalculés à part
# ==============================================================================
@pytest.fixture(scope="module")
def etat_de_la_journee(contrat, tmp_path_factory):
    etat = Etat()
    jouer(contrat, charger_catalogue(CHEMIN_CATALOGUE), journee_multi_usines(contrat), tmp_path_factory.mktemp("journee"), etat)
    return etat


def _jugeables_par_origine(registre):
    """Les livraisons dont des lignes ont été lues, par origine, de la plus ancienne à la plus récente : recomptées sans le code testé."""
    par_origine = {}
    for ligne in registre:
        if ligne.pays and ligne.statut in {ACCEPTEE, AVEC_REJETS, BLOQUEE} and ligne.nb_recues > 0:
            par_origine.setdefault((ligne.pays, ligne.usine, ligne.ligne), []).append(ligne)
    return {origine: sorted(lignes, key=lambda ligne: ligne.heure_arrivee) for origine, lignes in par_origine.items()}


def test_la_journee_a_de_quoi_etre_surveillee(etat_de_la_journee):
    """Sans une origine qui a au moins deux livraisons lues, les tests qui suivent ne prouveraient rien."""
    assert any(len(lignes) >= 2 for lignes in _jugeables_par_origine(etat_de_la_journee.registre).values())


def test_la_journee_jouee_a_des_volumes_stables_donc_aucun_constat(etat_de_la_journee):
    assert surveillance.controler_volumes(etat_de_la_journee.registre, _parametres_de(1, 0.5)) == []


def test_un_volume_qui_change_dans_la_journee_ne_signale_que_la_source_concernee(etat_de_la_journee):
    registre = list(etat_de_la_journee.registre)
    origine, lignes = next((o, ls) for o, ls in sorted(_jugeables_par_origine(registre).items()) if len(ls) >= 2)
    derniere = lignes[-1]
    registre[registre.index(derniere)] = dataclasses.replace(derniere, nb_recues=derniere.nb_recues * 3)

    constats = surveillance.controler_volumes(registre, _parametres_de(1, 0.5))

    assert [((c.pays, c.usine, c.ligne), c.sens, c.volume) for c in constats] == [(origine, "hausse", derniere.nb_recues * 3)]
