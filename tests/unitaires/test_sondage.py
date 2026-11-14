"""Tests du SONDAGE : la première exécution sur un workspace, strictement en lecture seule.

Le sondage est le petit programme que la personne lance elle-même
(`python -m rd_manuf.sondage --profil … --entrepot …`). Il fait deux choses, sans jamais rien créer ni écrire :

1. des ESSAIS de conversion : de simples `SELECT` sur des valeurs écrites dans la requête, dont on compare le résultat aux valeurs Python
   attendues. Ils prouvent ce que la #43 n'a pu que supposer (forme des instants et des listes rendus par le service, paramètres nommés,
   mode d'échec immédiat de `from_json`) ;
2. la PRÉSENCE de nos trois schémas et de nos cinq tables dans le catalogue playground, pour voir s'il existe déjà des tables de mêmes noms
   avant d'en créer.

Ces tests utilisent un faux exécuteur : rien ne part sur le réseau.
Ce que le service répond vraiment ne se prouve que par un lancement réel.
"""
import copy
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from rd_manuf.executeur_databricks import ErreurService
from rd_manuf.sondage import ESSAIS, Presence, ResultatEssai, jouer_les_essais, principal, verifier_la_presence

RACINE_DEPOT = Path(__file__).resolve().parents[2]
CHEMIN_CONTRAT = RACINE_DEPOT / "contracts" / "data" / "livraison_production.yaml"
PLAYGROUND = "rd_manuf_playground"

SCHEMAS_ATTENDUS = {"ops", "bronze", "silver"}
TABLES_ATTENDUES = {"ops.registre", "bronze.lignes", "bronze.lignes_coupees", "silver.lignes", "silver.quarantaine"}

# Ce que chaque essai doit attendre, écrit ici À PART de `ESSAIS` : le faux workspace parfait répond avec `ESSAIS`,
# ce tableau garde donc les attentes.
ATTENDUS = {
    "entier": 42,
    "decimal": 1.5,
    "date": date(2026, 11, 14),
    "instant": datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
    "instant_avec_decalage": datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
    "vide": None,
    "liste": ["a", "b"],
    "structure": {"code": "E1", "colonnes": ["x"]},
    "parametre": "é'\"\\\n",
    "from_json_valide": [{"a": 1}],
}
ESSAI_QUI_DOIT_ECHOUER = "from_json_illisible"
# Le refus de `from_json` en mode d'échec immédiat, selon la documentation Databricks (classe d'erreur MALFORMED_RECORD_IN_PARSING).
MESSAGE_DE_FAILFAST = "La requête a fini à l'état FAILED : [MALFORMED_RECORD_IN_PARSING.WITHOUT_SUGGESTION] Malformed records are detected."
INTERDITS_EN_LECTURE_SEULE = re.compile(
    r"\b(CREATE|INSERT|MERGE|DROP|DELETE|UPDATE|ALTER|TRUNCATE|GRANT|REVOKE|REPLACE|COPY|OPTIMIZE|VACUUM|USE|SET)\b", re.IGNORECASE
)


# ==============================================================================
# 🧰 LE FAUX WORKSPACE : répond à chaque essai comme on l'attend, et note tout ce qu'on lui envoie
# ==============================================================================
class FauxExecuteur:
    def __init__(self, reponse=None, *, schemas=(), tables=()):
        self.schemas, self.tables = tuple(schemas), tuple(tables)
        self.reponse = reponse or self._parfaite
        self.envoyes = []

    def __call__(self, sql, parametres):
        self.envoyes.append((sql, dict(parametres)))
        return self.reponse(sql, parametres)

    def _parfaite(self, sql, parametres):
        if sql == "SELECT 1 AS v":                      # la vérification de connexion, avant les essais
            return [{"v": 1}]
        essai = next((e for e in ESSAIS if e.sql == sql and e.parametres == parametres), None)
        if essai is not None:
            if essai.attendu is ErreurService:
                raise ErreurService(MESSAGE_DE_FAILFAST)
            return copy.deepcopy(essai.attendu)
        if "information_schema.schemata" in sql:
            return [{"schema_name": nom} for nom in self.schemas]
        if "information_schema.tables" in sql:
            return [{"table_schema": schema, "table_name": table} for schema, table in self.tables]
        raise AssertionError(f"requête inattendue : {sql}")


def _essai(nom):
    return next(essai for essai in ESSAIS if essai.nom == nom)


def _repond_sauf(nom, reponse):
    """Un faux workspace parfait, sauf pour l'essai `nom`, dont la réponse est `reponse` (une valeur, ou une fonction qui lève)."""
    parfait = FauxExecuteur()
    cible = _essai(nom)

    def par_essai(sql, parametres):
        if sql == cible.sql and parametres == cible.parametres:
            return reponse(sql, parametres) if callable(reponse) else reponse
        return parfait._parfaite(sql, parametres)

    return FauxExecuteur(par_essai)


def _leve(erreur):
    def lever(sql, parametres):
        raise erreur
    return lever


@pytest.fixture
def sortie():
    return []       # les lignes imprimées par le lanceur


def _lancer(argv, fabriquer, lignes, **options):
    return principal(argv, fabriquer=fabriquer, sortie=lignes.append, **options)


def _lancer_en_cas_d_interruption(argv, fabriquer, lignes):
    """Si le lanceur laissait passer l'interruption, pytest abandonnerait toute la session : on la transforme en échec lisible."""
    try:
        return _lancer(argv, fabriquer, lignes)
    except KeyboardInterrupt:
        pytest.fail("le lanceur n'a pas rattrapé l'interruption")


class Cadran:
    """Une horloge qui avance de `pas` secondes à chaque lecture : la durée d'un bloc mesuré par deux lectures vaut toujours `pas`."""

    def __init__(self, pas=1.5):
        self.instant, self.pas = 100.0, pas

    def __call__(self):
        self.instant += self.pas
        return self.instant


ARGUMENTS = ["--profil", "mon-profil", "--entrepot", "abc123", "--contrat", str(CHEMIN_CONTRAT)]


# ==============================================================================
# 🧪 LES ESSAIS : ce qu'ils demandent, et ce qu'ils en concluent
# ==============================================================================
def test_les_essais_ont_chacun_un_nom_et_il_y_en_a_assez():
    noms = [essai.nom for essai in ESSAIS]

    assert len(noms) == len(set(noms))
    assert set(noms) == set(ATTENDUS) | {ESSAI_QUI_DOIT_ECHOUER}


@pytest.mark.parametrize("nom", sorted(ATTENDUS))
def test_chaque_essai_attend_la_bonne_valeur(nom):
    assert _essai(nom).attendu == [{"v": ATTENDUS[nom]}]


def test_l_essai_de_from_json_illisible_attend_un_echec_du_service():
    assert _essai(ESSAI_QUI_DOIT_ECHOUER).attendu is ErreurService


def test_l_essai_de_from_json_prend_le_meme_mode_que_l_ecrivain():
    """L'essai ne vaut que s'il demande la même chose que `MagasinSql` : mode d'échec immédiat, valeurs en paramètre."""
    for nom in ("from_json_valide", ESSAI_QUI_DOIT_ECHOUER):
        essai = _essai(nom)

        assert "FAILFAST" in essai.sql and ":lignes" in essai.sql and "lignes" in essai.parametres


def test_l_essai_du_parametre_envoie_la_valeur_en_parametre_pas_dans_le_texte():
    essai = _essai("parametre")

    assert essai.parametres == {"texte": ATTENDUS["parametre"]}
    assert ATTENDUS["parametre"] not in essai.sql


def test_un_workspace_conforme_reussit_tous_les_essais():
    resultats = jouer_les_essais(FauxExecuteur())

    assert [r.nom for r in resultats] == [essai.nom for essai in ESSAIS]
    assert all(isinstance(r, ResultatEssai) and r.reussi for r in resultats)


def test_un_essai_est_envoye_avec_ses_parametres_tels_quels():
    faux = FauxExecuteur()

    jouer_les_essais(faux)

    assert faux.envoyes == [(essai.sql, essai.parametres) for essai in ESSAIS]


def test_une_valeur_differente_est_un_ecart_et_les_autres_essais_continuent():
    faux = _repond_sauf("instant", [{"v": datetime(2026, 11, 14, 14, 0, tzinfo=UTC)}])

    resultats = {r.nom: r for r in jouer_les_essais(faux)}

    assert list(resultats) == [essai.nom for essai in ESSAIS]
    assert not resultats["instant"].reussi
    assert "attendu" in resultats["instant"].detail and "obtenu" in resultats["instant"].detail
    assert all(r.reussi for nom, r in resultats.items() if nom != "instant")


def test_un_instant_sans_fuseau_rendu_en_naif_n_est_pas_pris_pour_de_l_utc():
    """Un `datetime` naïf ne vaut jamais un instant en UTC : l'égalité est fausse en Python, et l'essai le dit."""
    faux = _repond_sauf("instant", [{"v": datetime(2026, 11, 14, 12, 0)}])

    assert not {r.nom: r for r in jouer_les_essais(faux)}["instant"].reussi


ERREURS_DE_SONDE = [ErreurService("La requête a fini à l'état FAILED : mal écrite."), ValueError("illisible"), OSError("réseau")]


@pytest.mark.parametrize("erreur", ERREURS_DE_SONDE)
def test_une_erreur_sur_un_essai_est_rapportee_sans_arreter_les_autres(erreur):
    faux = _repond_sauf("entier", _leve(erreur))

    resultats = {r.nom: r for r in jouer_les_essais(faux)}

    assert list(resultats) == [essai.nom for essai in ESSAIS]
    assert not resultats["entier"].reussi
    assert type(erreur).__name__ in resultats["entier"].detail
    assert all(r.reussi for nom, r in resultats.items() if nom != "entier")


def test_l_echec_attendu_de_from_json_est_une_reussite_et_garde_le_message_du_service():
    resultat = {r.nom: r for r in jouer_les_essais(FauxExecuteur())}[ESSAI_QUI_DOIT_ECHOUER]

    assert resultat.reussi
    assert "FAILED" in resultat.detail


def test_from_json_qui_accepte_une_entree_illisible_est_un_ecart():
    """C'est le mode permissif par défaut : une valeur illisible deviendrait NULL sans erreur. Le sondage doit le signaler."""
    faux = _repond_sauf(ESSAI_QUI_DOIT_ECHOUER, [{"v": [{"a": None}]}])

    resultat = {r.nom: r for r in jouer_les_essais(faux)}[ESSAI_QUI_DOIT_ECHOUER]

    assert not resultat.reussi
    assert "aurait dû échouer" in resultat.detail


@pytest.mark.parametrize("message", [
    "La requête a fini à l'état FAILED : [PARSE_SYNTAX_ERROR] Syntax error at or near 'map'.",
    "La requête a fini à l'état CANCELED (sans message du service).",
    "La requête n'a pas fini dans le délai de 600 s : elle a été annulée.",
    "[WAREHOUSE_NOT_FOUND] L'entrepôt est introuvable.",
])
def test_un_autre_refus_du_service_n_est_pas_le_refus_attendu_de_from_json(message):
    """Une requête mal écrite, annulée ou refusée n'a rien prouvé du mode d'échec immédiat : ce serait un faux vert sur ce qu'on vérifie."""
    faux = _repond_sauf(ESSAI_QUI_DOIT_ECHOUER, _leve(ErreurService(message)))

    resultat = {r.nom: r for r in jouer_les_essais(faux)}[ESSAI_QUI_DOIT_ECHOUER]

    assert not resultat.reussi
    assert message.split(" : ")[-1][:40] in resultat.detail          # le message est montré, pour qu'on voie de quoi il s'agit


@pytest.mark.parametrize("message", [
    "[MALFORMED_RECORD_IN_PARSING.WITHOUT_SUGGESTION]",
    "malformed records are detected in record parsing",
    "Malformed Records Are Detected In Record Parsing. Parse Mode: FAILFAST.",
])
def test_le_refus_d_un_enregistrement_mal_forme_est_reconnu_sans_tenir_compte_de_la_casse(message):
    faux = _repond_sauf(ESSAI_QUI_DOIT_ECHOUER, _leve(ErreurService(message)))

    assert {r.nom: r for r in jouer_les_essais(faux)}[ESSAI_QUI_DOIT_ECHOUER].reussi


@pytest.mark.parametrize("message", ["Malformed " + "x" * 5000, "x" * 5000])
def test_le_detail_d_un_message_du_service_est_borne(message):
    faux = _repond_sauf(ESSAI_QUI_DOIT_ECHOUER, _leve(ErreurService(message)))

    resultat = {r.nom: r for r in jouer_les_essais(faux)}[ESSAI_QUI_DOIT_ECHOUER]

    assert len(resultat.detail) < 500


def test_une_reponse_vide_ou_en_trop_est_un_ecart():
    for reponse in ([], [{"v": 42}, {"v": 42}]):
        resultat = {r.nom: r for r in jouer_les_essais(_repond_sauf("entier", reponse))}["entier"]

        assert not resultat.reussi


def test_une_ligne_avec_une_colonne_en_trop_est_un_ecart():
    faux = _repond_sauf("decimal", [{"v": 1.5, "en_trop": 1}])

    assert not {r.nom: r for r in jouer_les_essais(faux)}["decimal"].reussi


@pytest.mark.parametrize("rendu", [42.0, True])
def test_un_type_voisin_n_est_pas_un_entier(rendu):
    """En Python `42.0 == 42` et `True == 1` : sans comparer les types, l'un rendu pour l'autre passerait pour conforme."""
    faux = _repond_sauf("from_json_valide", [{"v": [{"a": rendu}]}])

    assert not {r.nom: r for r in jouer_les_essais(faux)}["from_json_valide"].reussi


def test_un_decimal_rendu_pour_un_entier_est_un_ecart_au_premier_niveau():
    faux = _repond_sauf("entier", [{"v": 42.0}])

    assert not {r.nom: r for r in jouer_les_essais(faux)}["entier"].reussi


def test_une_panne_n_est_pas_le_refus_attendu_de_from_json():
    """Une panne de réseau sur l'essai d'échec attendu ne prouve rien du mode d'échec immédiat : seul un refus du service compte."""
    faux = _repond_sauf(ESSAI_QUI_DOIT_ECHOUER, _leve(OSError("réseau")))

    resultat = {r.nom: r for r in jouer_les_essais(faux)}[ESSAI_QUI_DOIT_ECHOUER]

    assert not resultat.reussi
    assert "OSError" in resultat.detail


# ==============================================================================
# 🔎 LA PRÉSENCE : nos trois schémas et nos cinq tables, rien d'autre
# ==============================================================================
def test_rien_n_existe_dans_un_catalogue_vide(contrat):
    presence = verifier_la_presence(contrat, PLAYGROUND, FauxExecuteur())

    assert isinstance(presence, Presence)
    assert presence.schemas == dict.fromkeys(SCHEMAS_ATTENDUS, False)
    assert presence.tables == dict.fromkeys(TABLES_ATTENDUES, False)


def test_les_schemas_et_tables_presents_sont_vus(contrat):
    faux = FauxExecuteur(schemas=["ops", "silver"], tables=[("ops", "registre"), ("silver", "lignes")])

    presence = verifier_la_presence(contrat, PLAYGROUND, faux)

    assert presence.schemas == {"ops": True, "bronze": False, "silver": True}
    assert presence.tables == {
        "ops.registre": True, "bronze.lignes": False, "bronze.lignes_coupees": False, "silver.lignes": True, "silver.quarantaine": False,
    }


def test_un_nom_qui_n_est_pas_a_nous_est_ignore_meme_dans_nos_schemas(contrat):
    """Le sondage ne nomme que nos 5 tables : une autre table d'un de nos schémas ne sort nulle part."""
    tables = [("ops", "registre"), ("ops", "autre_table"), ("autre", "lignes"), ("bronze", "registre")]
    faux = FauxExecuteur(schemas=["ops", "autre"], tables=tables)

    presence = verifier_la_presence(contrat, PLAYGROUND, faux)

    assert set(presence.schemas) == SCHEMAS_ATTENDUS and set(presence.tables) == TABLES_ATTENDUES
    assert presence.tables["ops.registre"] is True
    assert presence.tables["bronze.lignes"] is False        # `bronze.registre` et `autre.lignes` ne sont pas nos tables
    assert presence.tables["silver.lignes"] is False


def test_la_presence_interroge_le_catalogue_demande_et_seulement_nos_schemas(contrat):
    faux = FauxExecuteur()

    verifier_la_presence(contrat, PLAYGROUND, faux)

    assert len(faux.envoyes) == 2
    for sql, parametres in faux.envoyes:
        assert f"`{PLAYGROUND}`.information_schema." in sql
        assert parametres == {}
        for schema in SCHEMAS_ATTENDUS:
            assert f"'{schema}'" in sql


def test_la_presence_refuse_un_catalogue_inconnu_avant_tout_envoi(contrat):
    from rd_manuf.config import ErreurEnvironnement

    faux = FauxExecuteur()

    with pytest.raises(ErreurEnvironnement):
        verifier_la_presence(contrat, "space_1", faux)

    assert faux.envoyes == []


# ==============================================================================
# 🔒 LA LECTURE SEULE : rien d'envoyé ne crée, n'écrit, ni ne change quoi que ce soit
# ==============================================================================
def test_toute_requete_envoyee_est_un_select_sans_mot_d_ecriture(sortie):
    faux = FauxExecuteur(schemas=["ops"], tables=[("ops", "registre")])

    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)

    assert code == 0 and faux.envoyes
    for sql, parametres in faux.envoyes:
        assert re.match(r"\s*SELECT\b", sql, re.IGNORECASE), sql
        assert ";" not in sql, sql
        assert not INTERDITS_EN_LECTURE_SEULE.search(sql), sql
        assert all(isinstance(valeur, str) for valeur in parametres.values())


def test_les_requetes_de_presence_sont_aussi_en_lecture_seule(contrat):
    faux = FauxExecuteur()

    verifier_la_presence(contrat, PLAYGROUND, faux)

    for sql, _ in faux.envoyes:
        assert re.match(r"\s*SELECT\b", sql, re.IGNORECASE) and not INTERDITS_EN_LECTURE_SEULE.search(sql), sql


def test_le_sondage_n_ecrit_aucun_fichier(tmp_path, monkeypatch, sortie):
    monkeypatch.chdir(tmp_path)

    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie)

    assert list(tmp_path.iterdir()) == []


# ==============================================================================
# 🚀 LE LANCEUR : arguments, garde du catalogue, code de sortie
# ==============================================================================
def test_le_lanceur_transmet_le_profil_et_l_entrepot_a_la_fabrique(sortie):
    recus = []

    def fabriquer(profil, entrepot, attente_max=None):
        recus.append((profil, entrepot))
        return FauxExecuteur()

    _lancer(ARGUMENTS, fabriquer, sortie)

    assert recus == [("mon-profil", "abc123")]


def test_tout_conforme_sort_avec_zero_et_dit_que_rien_n_a_ete_ecrit(sortie):
    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie)
    texte = "\n".join(sortie)

    assert code == 0
    assert "Rien n'a été créé ni écrit" in sortie[-1]
    assert "ÉCART" not in texte


def test_un_ecart_sort_avec_un_et_nomme_l_essai(sortie):
    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: _repond_sauf("liste", [{"v": ["a"]}]), sortie)
    texte = "\n".join(sortie)

    assert code == 1
    assert re.search(r"ÉCART\s+liste : attendu \[\{'v': \['a', 'b'\]\}\], obtenu \[\{'v': \['a'\]\}\]", texte)
    assert re.search(r"OK\s+entier", texte)
    assert re.search(r"OK\s+from_json_illisible : le service a refusé comme prévu : .*FAILED", texte)


def test_le_sondage_dit_pour_chaque_schema_et_chaque_table_s_il_existe(sortie):
    faux = FauxExecuteur(schemas=["ops"], tables=[("ops", "registre")])

    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)
    texte = "\n".join(sortie)

    assert re.search(r"schéma ops\s*: présent", texte) and re.search(r"schéma bronze\s*: absent", texte)
    assert re.search(r"table ops\.registre\s*: présente \(table ou vue\)", texte)
    assert re.search(r"table silver\.quarantaine\s*: absente", texte)


def test_la_sortie_dit_que_absent_veut_dire_absent_pour_ce_profil(sortie):
    """`information_schema` ne montre que ce sur quoi le profil a des droits : un « absent » n'est pas une preuve d'absence."""
    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie)

    note = "\n".join(ligne for ligne in sortie if "profil" in ligne)

    assert "absent pour ce profil" in note and "droits" in note


def test_un_echec_de_la_verification_de_presence_sort_avec_un_et_dit_pourquoi(sortie):
    def reponse(sql, parametres):
        if "information_schema" in sql:
            raise ErreurService("La requête a fini à l'état FAILED : catalogue introuvable.")
        return FauxExecuteur()._parfaite(sql, parametres)

    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(reponse), sortie)

    assert code == 1
    assert "catalogue introuvable" in "\n".join(sortie)


def test_la_connexion_est_verifiee_par_une_requete_avant_les_essais(sortie):
    faux = FauxExecuteur()

    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)

    assert faux.envoyes[0] == ("SELECT 1 AS v", {})
    assert [sql for sql, _ in faux.envoyes[1:3]] == [ESSAIS[0].sql, ESSAIS[1].sql]


@pytest.mark.parametrize("erreur", [
    ErreurService("La requête a fini à l'état FAILED : entrepôt introuvable."),
    PermissionError("jeton expiré"),
])
def test_une_requete_de_connexion_qui_echoue_sort_avec_deux_au_lieu_de_onze_ecarts(erreur, sortie):
    """Un jeton expiré ou un mauvais identifiant d'entrepôt échoue sur CHAQUE requête : un seul message, pas onze écarts."""
    faux = FauxExecuteur(_leve(erreur))

    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)
    texte = "\n".join(sortie)

    assert code == 2
    assert len(faux.envoyes) == 1
    assert "Connexion impossible" in texte and type(erreur).__name__ in texte
    assert "ÉCART" not in texte and "présent" not in texte


ERREURS_DE_CONNEXION = [RuntimeError("Le SDK Databricks n'est pas installé."), ValueError("profil introuvable"), OSError("réseau")]


@pytest.mark.parametrize("erreur", ERREURS_DE_CONNEXION)
def test_une_connexion_impossible_sort_avec_deux_sans_jouer_d_essai(erreur, sortie):
    def fabriquer(profil, entrepot, attente_max=None):
        raise erreur

    code = _lancer(ARGUMENTS, fabriquer, sortie)
    texte = "\n".join(sortie)

    assert code == 2
    assert "Connexion impossible" in texte and type(erreur).__name__ in texte
    assert "OK" not in texte and "ÉCART" not in texte


@pytest.mark.parametrize("catalogue", ["rd_manuf_dev", "rd_manuf_prod", "space_1"])
def test_un_catalogue_autre_que_le_playground_est_refuse_avant_toute_connexion(catalogue, sortie):
    appels = []

    code = _lancer([*ARGUMENTS, "--catalogue", catalogue], lambda profil, entrepot, attente_max=None: appels.append(1), sortie)

    assert code == 2
    assert appels == []
    assert "playground" in "\n".join(sortie)


def test_le_catalogue_par_defaut_est_le_playground(sortie):
    faux = FauxExecuteur()

    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)

    assert any(f"`{PLAYGROUND}`" in sql for sql, _ in faux.envoyes)


@pytest.mark.parametrize("manquant", ["--profil", "--entrepot"])
def test_profil_et_entrepot_sont_obligatoires(manquant, sortie):
    argv = list(ARGUMENTS)
    position = argv.index(manquant)
    del argv[position:position + 2]

    with pytest.raises(SystemExit) as sortie_argparse:
        _lancer(argv, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie)

    assert sortie_argparse.value.code == 2


def test_le_contrat_par_defaut_est_celui_du_depot(monkeypatch, sortie):
    monkeypatch.chdir(RACINE_DEPOT)
    argv = ["--profil", "mon-profil", "--entrepot", "abc123"]

    assert _lancer(argv, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie) == 0


def test_un_contrat_introuvable_sort_avec_deux_sans_connexion(tmp_path, sortie):
    appels = []
    argv = ["--profil", "mon-profil", "--entrepot", "abc123", "--contrat", str(tmp_path / "absent.yaml")]

    code = _lancer(argv, lambda profil, entrepot, attente_max=None: appels.append(1), sortie)

    assert code == 2 and appels == []
    assert "racine du dépôt" in "\n".join(sortie) and "--contrat" in "\n".join(sortie)


# ==============================================================================
# 📦 LE MODULE : lancé comme programme, importable sans le SDK, fabrique réelle
# ==============================================================================
def test_le_module_se_lance_en_programme_et_affiche_son_aide():
    resultat = subprocess.run([sys.executable, "-m", "rd_manuf.sondage", "--help"], capture_output=True, text=True, check=False)

    assert resultat.returncode == 0
    assert "--profil" in resultat.stdout and "--entrepot" in resultat.stdout


def test_importer_le_module_n_importe_pas_le_sdk():
    code = "import sys, rd_manuf.sondage; sys.exit(1 if any(m == 'databricks' or m.startswith('databricks.') for m in sys.modules) else 0)"

    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_la_fabrique_reelle_cree_le_client_avec_le_profil_demande(monkeypatch):
    pytest.importorskip("databricks.sdk")
    import databricks.sdk

    from rd_manuf.executeur_databricks import ExecuteurDatabricks
    from rd_manuf.sondage import fabriquer_executeur_reel

    clients = []

    class FauxClient:
        def __init__(self, **options):
            clients.append(options)

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", FauxClient)

    executeur = fabriquer_executeur_reel("mon-profil", "abc123")

    assert clients == [{"profile": "mon-profil"}]
    assert isinstance(executeur, ExecuteurDatabricks)


COMMANDE_D_INSTALLATION = 'pip install -e ".[databricks]"'


def test_le_message_du_sdk_absent_donne_la_meme_commande_que_le_readme(monkeypatch):
    from rd_manuf.sondage import fabriquer_executeur_reel

    monkeypatch.setitem(sys.modules, "databricks.sdk", None)         # « import databricks.sdk » échoue comme si le SDK était absent

    with pytest.raises(RuntimeError) as erreur:
        fabriquer_executeur_reel("mon-profil", "abc123")

    assert COMMANDE_D_INSTALLATION in str(erreur.value)
    assert COMMANDE_D_INSTALLATION in (RACINE_DEPOT / "README.md").read_text(encoding="utf-8")


# ==============================================================================
# 🔗 LA LECTURE JSON DU SONDAGE EST CELLE DE L'ÉCRIVAIN
# ==============================================================================
def _options_de_from_json(sql):
    trouve = re.search(r"from_json\(:lignes, '(?P<forme>[^']*)', (?P<options>map\([^)]*\))\)", sql)
    assert trouve, f"pas de lecture JSON reconnue dans : {sql}"
    return trouve.group("forme"), trouve.group("options")


def test_le_sondage_lit_le_json_comme_le_magasin_sql():
    """Rien d'autre ne lie les deux : si l'écrivain changeait ses options de `from_json`, l'essai garderait l'ancienne forme."""
    from rd_manuf.ecriture import MagasinSql
    from rd_manuf.tables import REGISTRE

    envoyes = []
    magasin = MagasinSql(lambda sql, parametres: envoyes.append(sql) or [], PLAYGROUND)
    magasin.inserer_absentes(REGISTRE, (REGISTRE.colonnes[0].nom,), [dict.fromkeys((c.nom for c in REGISTRE.colonnes))])

    forme_de_l_ecrivain, options_de_l_ecrivain = _options_de_from_json(envoyes[0])
    for nom in ("from_json_valide", ESSAI_QUI_DOIT_ECHOUER):
        forme_de_l_essai, options_de_l_essai = _options_de_from_json(_essai(nom).sql)

        assert options_de_l_essai == options_de_l_ecrivain
        assert forme_de_l_essai.startswith("ARRAY<STRUCT<") and forme_de_l_ecrivain.startswith("ARRAY<STRUCT<")


# ==============================================================================
# ⏱️ L'ATTENTE : délai réglable, ligne d'attente, durées mesurées, causes à vérifier, interruption
# ==============================================================================
def test_le_delai_par_defaut_est_de_120_secondes_et_va_a_la_fabrique(sortie):
    recus = []

    def fabriquer(profil, entrepot, attente_max=None):
        recus.append(attente_max)
        return FauxExecuteur()

    _lancer(ARGUMENTS, fabriquer, sortie)

    assert recus == [120.0]


def test_l_argument_delai_est_transmis_a_la_fabrique(sortie):
    recus = []

    def fabriquer(profil, entrepot, attente_max=None):
        recus.append(attente_max)
        return FauxExecuteur()

    _lancer([*ARGUMENTS, "--delai", "30"], fabriquer, sortie)

    assert recus == [30.0]


@pytest.mark.parametrize("valeur", ["0", "-5", "abc", "nan", "inf"])
def test_un_delai_invalide_est_refuse_avant_toute_connexion(valeur, sortie, capsys):
    appels = []

    with pytest.raises(SystemExit) as sortie_argparse:
        _lancer([*ARGUMENTS, "--delai", valeur], lambda profil, entrepot, attente_max=None: appels.append(1), sortie)

    assert sortie_argparse.value.code == 2 and appels == []
    assert "fini et positif" in capsys.readouterr().err


def test_la_fabrique_reelle_donne_le_delai_a_l_executeur(monkeypatch):
    pytest.importorskip("databricks.sdk")
    import databricks.sdk

    from rd_manuf import sondage
    from rd_manuf.sondage import fabriquer_executeur_reel

    recus = {}

    class FauxExecuteurDatabricks:
        def __init__(self, client, entrepot, **options):
            recus.update(entrepot=entrepot, **options)

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", lambda **options: object())
    monkeypatch.setattr(sondage, "ExecuteurDatabricks", FauxExecuteurDatabricks)

    fabriquer_executeur_reel("mon-profil", "abc123", attente_max=45.0)

    assert recus == {"entrepot": "abc123", "attente_max": 45.0}


def test_le_lanceur_dit_qu_il_se_connecte_avant_d_envoyer_la_premiere_requete(sortie):
    vu_avant_l_envoi = []
    faux = FauxExecuteur()

    def espion(sql, parametres):
        if not vu_avant_l_envoi:
            vu_avant_l_envoi.extend(sortie)
        return faux(sql, parametres)

    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: espion, sortie)

    assert any("Connexion à l'entrepôt" in ligne for ligne in vu_avant_l_envoi)


def test_la_ligne_d_attente_est_affichee_meme_si_la_connexion_echoue(sortie):
    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(_leve(PermissionError("jeton expiré"))), sortie)

    assert any("Connexion à l'entrepôt" in ligne for ligne in sortie)


def test_la_duree_de_la_connexion_et_de_chaque_essai_est_affichee(sortie):
    _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(), sortie, horloge=Cadran(1.5))
    texte = "\n".join(sortie)

    assert re.search(r"Connexion établie \(1,5 s\)", texte)
    assert re.search(r"^OK\s+entier \(1,5 s\)$", texte, re.MULTILINE)
    assert len(re.findall(r"\(1,5 s\)$", texte, re.MULTILINE)) == len(ESSAIS) + 1


def test_la_duree_d_un_essai_en_ecart_est_affichee_apres_son_detail(sortie):
    code = _lancer(
        ARGUMENTS, lambda profil, entrepot, attente_max=None: _repond_sauf("liste", [{"v": ["a"]}]), sortie, horloge=Cadran(2.25)
    )

    assert code == 1
    assert re.search(r"^ÉCART\s+liste : attendu .* \(2,2 s\)$", "\n".join(sortie), re.MULTILINE)


def test_chaque_essai_porte_sa_duree_mesuree_sur_l_horloge_donnee():
    resultats = jouer_les_essais(FauxExecuteur(), horloge=Cadran(1.5))

    assert [r.duree for r in resultats] == [1.5] * len(ESSAIS)


def test_une_connexion_impossible_liste_les_causes_a_verifier(sortie):
    code = _lancer(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(_leve(PermissionError("jeton expiré"))), sortie)
    texte = "\n".join(sortie)

    assert code == 2
    for cause in ("profil", "identifiant de l'entrepôt", "SQL Warehouses", "quota"):
        assert cause in texte, cause


def test_une_connexion_impossible_dit_apres_combien_de_temps(sortie):
    code = _lancer(
        ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(_leve(PermissionError("jeton expiré"))), sortie,
        horloge=Cadran(1.5),
    )

    assert code == 2
    assert re.search(r"^Connexion impossible après 1,5 s : PermissionError : jeton expiré$", "\n".join(sortie), re.MULTILINE)


def test_une_autre_runtimeerror_garde_les_causes_a_verifier(sortie):
    """Seul le SDK absent les supprime : une panne quelconque, même de type `RuntimeError`, doit les afficher."""
    def fabriquer(profil, entrepot, attente_max=None):
        raise RuntimeError("panne quelconque")

    code = _lancer(ARGUMENTS, fabriquer, sortie)
    texte = "\n".join(sortie)

    assert code == 2
    assert "panne quelconque" in texte and "À vérifier" in texte and "quota" in texte


def test_un_sdk_absent_dit_comment_l_installer_sans_pister_le_profil_ni_le_quota(monkeypatch, sortie):
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)         # « import databricks.sdk » échoue comme si le SDK était absent

    code = principal(ARGUMENTS, sortie=sortie.append)             # la fabrique réelle, pas un faux
    texte = "\n".join(sortie)

    assert code == 2
    assert COMMANDE_D_INSTALLATION in texte
    assert "À vérifier" not in texte and "quota" not in texte


def test_une_interruption_pendant_la_connexion_sort_avec_130_sans_se_dire_une_panne(sortie):
    def interrompu(sql, parametres):
        raise KeyboardInterrupt

    code = _lancer_en_cas_d_interruption(ARGUMENTS, lambda profil, entrepot, attente_max=None: FauxExecuteur(interrompu), sortie)
    texte = "\n".join(sortie)

    assert code == 130
    assert "Interrompu" in texte and "Connexion impossible" not in texte


def test_une_interruption_pendant_les_essais_arrete_les_suivants_et_sort_avec_130(sortie):
    faux = FauxExecuteur()

    def reponse(sql, parametres):
        if len(faux.envoyes) == 3:                  # la connexion, le premier essai, puis le deuxième est interrompu
            raise KeyboardInterrupt
        return faux._parfaite(sql, parametres)

    faux.reponse = reponse

    code = _lancer_en_cas_d_interruption(ARGUMENTS, lambda profil, entrepot, attente_max=None: faux, sortie)
    texte = "\n".join(sortie)

    assert code == 130 and len(faux.envoyes) == 3
    assert "Interrompu" in texte and "schéma" not in texte
