"""Tests de l'EXÉCUTEUR Databricks, avec les VRAIS objets du SDK et un faux service : rien ne part sur le réseau.

L'exécuteur est le seul point de contact entre l'écrivain (`MagasinSql`) et un entrepôt SQL : il envoie la requête et ses paramètres
nommés, attend la fin, suit les morceaux du résultat et rend des lignes aux types Python des schémas. Le faux service rejoue des réponses
construites avec les classes du SDK (`StatementResponse` et ses voisines) et note tout ce qu'on lui envoie ; l'horloge et le sommeil sont
injectés, donc aucun test n'attend pour de vrai.

Ces tests prouvent le protocole d'envoi et la lecture des réponses. Ce que le service rend vraiment (forme des instants, des listes, durée,
acceptation de l'appel par la Free Edition) ne se prouve que sur un workspace : voir le journal des changements.
Sans le SDK installé, tout ce fichier est ignoré (la CI l'installe, et un test du fichier voisin vérifie qu'il y est).
"""
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import create_autospec

import pytest

pytest.importorskip("databricks.sdk")

from databricks.sdk.service.sql import (  # noqa: E402
    ColumnInfo,
    ColumnInfoTypeName,
    Disposition,
    ExecuteStatementRequestOnWaitTimeout,
    Format,
    ResultData,
    ResultManifest,
    ResultSchema,
    ServiceError,
    StatementExecutionAPI,
    StatementParameterListItem,
    StatementResponse,
    StatementState,
    StatementStatus,
)

from rd_manuf.alertes import charger_catalogue  # noqa: E402
from rd_manuf.config import CATALOGUE_PAR_DEFAUT  # noqa: E402
from rd_manuf.ecriture import MagasinSql, couches  # noqa: E402
from rd_manuf.executeur_databricks import ErreurService, ExecuteurDatabricks  # noqa: E402
from rd_manuf.journee import Etat, jouer, journee_multi_usines  # noqa: E402

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
VALEUR_SECRETE = "VALEUR_SECRETE"
TYPES_DU_SERVICE = {"STRING": "STRING", "BIGINT": "LONG", "DOUBLE": "DOUBLE", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}


# ==============================================================================
# 🧰 LE FAUX SERVICE : de vraies réponses du SDK, un journal de ce qui est demandé
# ==============================================================================
def _colonne(nom, type_name, type_text, position=0):
    return ColumnInfo(name=nom, position=position, type_name=ColumnInfoTypeName(type_name), type_text=type_text)


def _reponse(etat, colonnes=None, lignes=None, *, identifiant="s1", message=None, tronque=False, suivant=None):
    manifest = None
    if colonnes is not None:
        manifest = ResultManifest(schema=ResultSchema(column_count=len(colonnes), columns=colonnes), truncated=tronque, total_chunk_count=1)
    resultat = None if lignes is None else ResultData(data_array=lignes, chunk_index=0, next_chunk_index=suivant, row_count=len(lignes))
    erreur = ServiceError(message=message) if message else None
    return StatementResponse(statement_id=identifiant, status=StatementStatus(state=etat, error=erreur), manifest=manifest, result=resultat)


class FauxService:
    """`client.statement_execution` : rejoue des réponses et note ce qu'on lui envoie."""

    def __init__(self, premiere, suivantes=(), morceaux=None):
        self.premiere = premiere
        self.suivantes = list(suivantes)
        self.morceaux = dict(morceaux or {})
        self.envois, self.interrogations, self.annulations, self.morceaux_demandes = [], [], [], []

    def execute_statement(self, **options):
        self.envois.append(options)
        return self.premiere

    def get_statement(self, statement_id):
        self.interrogations.append(statement_id)
        return self.suivantes.pop(0)

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        self.morceaux_demandes.append((statement_id, chunk_index))
        return self.morceaux[chunk_index]

    def cancel_execution(self, statement_id):
        self.annulations.append(statement_id)


class FauxClient:
    def __init__(self, service):
        self.statement_execution = service


DEPART = 1000.0        # l'horloge ne démarre pas à zéro : un début de délai écrit en dur ne doit pas passer pour un début lu sur l'horloge


class Horloge:
    """Le temps et le sommeil injectés : dormir fait avancer l'horloge sans attendre."""

    def __init__(self):
        self.instant = DEPART
        self.pauses = []

    def __call__(self):
        return self.instant

    def dormir(self, secondes):
        self.pauses.append(secondes)
        self.instant += secondes


def _executeur(service, horloge=None, **options):
    horloge = horloge or Horloge()
    return ExecuteurDatabricks(FauxClient(service), "entrepot-1", dormir=horloge.dormir, horloge=horloge, **options)


SUCCES = StatementState.SUCCEEDED


# ==============================================================================
# 1. 📤 CE QUI PART : le texte, les paramètres nommés, l'entrepôt, et rien d'autre
# ==============================================================================
def test_la_requete_part_avec_son_texte_ses_parametres_nommes_et_l_entrepot_et_rien_d_autre():
    service = FauxService(_reponse(SUCCES))

    _executeur(service)("MERGE INTO t USING (SELECT :lignes) ON x", {"lignes": '[{"a":1}]'})

    (envoi,) = service.envois
    assert envoi["statement"] == "MERGE INTO t USING (SELECT :lignes) ON x"
    assert envoi["warehouse_id"] == "entrepot-1"
    assert envoi["parameters"] == [StatementParameterListItem(name="lignes", value='[{"a":1}]', type="STRING")]
    assert envoi["wait_timeout"] == "50s"
    assert envoi["on_wait_timeout"] == ExecuteStatementRequestOnWaitTimeout.CONTINUE
    assert envoi["disposition"] == Disposition.INLINE and envoi["format"] == Format.JSON_ARRAY
    assert set(envoi) == {"statement", "warehouse_id", "parameters", "wait_timeout", "on_wait_timeout", "disposition", "format"}


def test_plusieurs_parametres_partent_dans_l_ordre_tous_en_texte():
    service = FauxService(_reponse(SUCCES))

    _executeur(service)("SELECT :a, :b", {"a": "1", "b": "deux"})

    assert service.envois[0]["parameters"] == [
        StatementParameterListItem(name="a", value="1", type="STRING"),
        StatementParameterListItem(name="b", value="deux", type="STRING"),
    ]


def test_sans_parametre_aucun_parametre_n_est_envoye():
    service = FauxService(_reponse(SUCCES))

    _executeur(service)("CREATE SCHEMA IF NOT EXISTS `c`.`s`", {})

    assert service.envois[0]["parameters"] is None


@pytest.mark.parametrize("valeur", [1, 1.5, None, ["a"], b"x"])
def test_un_parametre_qui_n_est_pas_du_texte_est_refuse_avant_tout_envoi(valeur):
    service = FauxService(_reponse(SUCCES))

    with pytest.raises(TypeError):
        _executeur(service)("SELECT :a", {"a": valeur})

    assert service.envois == []


def test_sans_le_sdk_l_envoi_dit_comment_l_installer_et_rien_ne_part(monkeypatch):
    service = FauxService(_reponse(SUCCES))
    monkeypatch.setitem(sys.modules, "databricks.sdk.service.sql", None)      # l'import du SDK échoue comme s'il n'était pas installé

    with pytest.raises(RuntimeError, match=r"rd-manuf\[databricks\]"):
        _executeur(service)("SELECT 1", {})

    assert service.envois == []


def test_l_entrepot_et_le_client_sont_ceux_qu_on_donne():
    service = FauxService(_reponse(SUCCES))

    ExecuteurDatabricks(FauxClient(service), "autre-entrepot")("SELECT 1", {})

    assert service.envois[0]["warehouse_id"] == "autre-entrepot"


# ==============================================================================
# 2. 📥 CE QUI REVIENT : des lignes aux types Python des colonnes
# ==============================================================================
COLONNES_VARIEES = [
    _colonne("id", "STRING", "STRING", 0),
    _colonne("n", "LONG", "BIGINT", 1),
    _colonne("x", "DOUBLE", "DOUBLE", 2),
    _colonne("jour", "DATE", "DATE", 3),
    _colonne("instant", "TIMESTAMP", "TIMESTAMP", 4),
    _colonne("liste", "ARRAY", "ARRAY<STRING>", 5),
    _colonne("table", "MAP", "MAP<STRING, STRING>", 6),
]


def test_les_lignes_rendues_ont_le_nom_et_le_type_de_leur_colonne_dans_l_ordre_recu():
    lignes = [
        ["a", "7", "1.5", "2026-11-14", "2026-11-14T10:00:00.000Z", '["u","v"]', '{"k":"w"}'],
        [None, None, None, None, None, None, None],
        ["b", "-3", "0.0", "2026-01-02", "2026-01-02T03:04:05.678901Z", "[]", "{}"],
    ]
    service = FauxService(_reponse(SUCCES, COLONNES_VARIEES, lignes))

    rendu = _executeur(service)("SELECT * FROM t", {})

    assert rendu == [
        {"id": "a", "n": 7, "x": 1.5, "jour": date(2026, 11, 14), "instant": datetime(2026, 11, 14, 10, 0, tzinfo=UTC), "liste": ["u", "v"],
         "table": {"k": "w"}},
        {"id": None, "n": None, "x": None, "jour": None, "instant": None, "liste": None, "table": None},
        {"id": "b", "n": -3, "x": 0.0, "jour": date(2026, 1, 2), "instant": datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC), "liste": [],
         "table": {}},
    ]
    assert type(rendu[0]["n"]) is int and type(rendu[0]["x"]) is float and type(rendu[2]["x"]) is float


@pytest.mark.parametrize(
    "reponse",
    [
        pytest.param(_reponse(SUCCES), id="ni-schema-ni-resultat"),
        pytest.param(_reponse(SUCCES, COLONNES_VARIEES), id="un-schema-sans-resultat"),
        pytest.param(_reponse(SUCCES, COLONNES_VARIEES, []), id="un-schema-et-aucune-ligne"),
        pytest.param(
            StatementResponse(statement_id="s1", status=StatementStatus(state=SUCCES), result=None,
                              manifest=ResultManifest(schema=None, truncated=False, total_chunk_count=1)),
            id="un-manifeste-sans-schema",
        ),
        pytest.param(
            StatementResponse(statement_id="s1", status=StatementStatus(state=SUCCES), result=ResultData(data_array=None, chunk_index=0),
                              manifest=ResultManifest(schema=ResultSchema(column_count=1, columns=[_colonne("n", "LONG", "BIGINT")]),
                                                      truncated=False, total_chunk_count=1)),
            id="un-resultat-sans-liste-de-valeurs",
        ),
    ],
)
def test_une_reponse_sans_ligne_rend_une_liste_vide(reponse):
    assert _executeur(FauxService(reponse))("CREATE SCHEMA x", {}) == []


def test_un_resultat_en_plusieurs_morceaux_est_suivi_jusqu_au_dernier_dans_l_ordre():
    colonnes = [_colonne("n", "LONG", "BIGINT")]
    service = FauxService(
        _reponse(SUCCES, colonnes, [["1"], ["2"]], suivant=1),
        morceaux={
            1: ResultData(data_array=[["3"]], chunk_index=1, next_chunk_index=2),
            2: ResultData(data_array=[["4"], ["5"]], chunk_index=2, next_chunk_index=None),
        },
    )

    rendu = _executeur(service)("SELECT n FROM t", {})

    assert [ligne["n"] for ligne in rendu] == [1, 2, 3, 4, 5]
    assert service.morceaux_demandes == [("s1", 1), ("s1", 2)]


def test_un_resultat_tronque_est_refuse_au_lieu_d_etre_rendu_incomplet():
    service = FauxService(_reponse(SUCCES, [_colonne("n", "LONG", "BIGINT")], [["1"]], tronque=True))

    with pytest.raises(ErreurService, match="tronqu"):
        _executeur(service)("SELECT n FROM t", {})


def test_une_valeur_illisible_est_refusee_avec_sa_colonne_et_son_type_sans_la_valeur():
    service = FauxService(_reponse(SUCCES, [_colonne("nb_recues", "LONG", "BIGINT")], [[VALEUR_SECRETE]]))

    with pytest.raises(ValueError) as erreur:
        _executeur(service)("SELECT nb_recues FROM t", {})

    message = str(erreur.value)
    assert "nb_recues" in message and "BIGINT" in message and VALEUR_SECRETE not in message and VALEUR_SECRETE not in repr(erreur.value)


def test_une_ligne_qui_n_a_pas_autant_de_valeurs_que_de_colonnes_est_refusee():
    service = FauxService(_reponse(SUCCES, [_colonne("a", "STRING", "STRING", 0), _colonne("b", "STRING", "STRING", 1)], [["x"]]))

    with pytest.raises(ValueError, match="colonnes"):
        _executeur(service)("SELECT a, b FROM t", {})


def test_une_colonne_d_un_type_que_le_code_ne_produit_pas_est_refusee():
    service = FauxService(_reponse(SUCCES, [_colonne("b", "BOOLEAN", "BOOLEAN")], [["true"]]))

    with pytest.raises(ValueError, match="BOOLEAN"):
        _executeur(service)("SELECT b FROM t", {})


# ==============================================================================
# 3. ⏳ L'ATTENTE : interroger tant que ça tourne, annuler au bout du délai
# ==============================================================================
def test_une_requete_deja_terminee_n_est_ni_interrogee_ni_attendue():
    horloge = Horloge()
    service = FauxService(_reponse(SUCCES, [_colonne("n", "LONG", "BIGINT")], [["1"]]))

    _executeur(service, horloge)("SELECT 1", {})

    assert service.interrogations == [] and horloge.pauses == []


def test_une_requete_en_cours_est_interrogee_apres_chaque_pause_jusqu_a_sa_fin():
    horloge = Horloge()
    colonnes = [_colonne("n", "LONG", "BIGINT")]
    service = FauxService(
        _reponse(StatementState.PENDING),
        suivantes=[_reponse(StatementState.RUNNING), _reponse(SUCCES, colonnes, [["9"]])],
    )

    rendu = _executeur(service, horloge, pause=2.0)("SELECT 9", {})

    assert rendu == [{"n": 9}]
    assert service.interrogations == ["s1", "s1"] and horloge.pauses == [2.0, 2.0]


@pytest.mark.parametrize("etat", [StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED])
def test_une_requete_qui_n_aboutit_pas_leve_une_erreur_qui_nomme_l_etat_et_le_message_du_service(etat):
    service = FauxService(_reponse(etat, message="TABLE_OR_VIEW_NOT_FOUND : t"))

    with pytest.raises(ErreurService) as erreur:
        _executeur(service)("SELECT 1 FROM t", {})

    assert etat.value in str(erreur.value) and "TABLE_OR_VIEW_NOT_FOUND" in str(erreur.value)


def test_une_requete_qui_echoue_apres_avoir_attendu_leve_la_meme_erreur():
    service = FauxService(_reponse(StatementState.RUNNING), suivantes=[_reponse(StatementState.FAILED, message="boum")])

    with pytest.raises(ErreurService, match="FAILED.*boum"):
        _executeur(service)("SELECT 1", {})


def test_un_echec_sans_message_leve_quand_meme_une_erreur_qui_nomme_l_etat():
    service = FauxService(_reponse(StatementState.FAILED))

    with pytest.raises(ErreurService, match="FAILED"):
        _executeur(service)("SELECT 1", {})


def test_aucune_erreur_du_service_ne_contient_les_valeurs_envoyees():
    service = FauxService(_reponse(StatementState.FAILED, message="syntaxe"))

    with pytest.raises(ErreurService) as erreur:
        _executeur(service)("SELECT :lignes", {"lignes": VALEUR_SECRETE})

    assert VALEUR_SECRETE not in str(erreur.value) and VALEUR_SECRETE not in repr(erreur.value)


def test_une_requete_qui_depasse_le_delai_est_annulee_puis_refusee():
    horloge = Horloge()
    service = FauxService(_reponse(StatementState.RUNNING), suivantes=[_reponse(StatementState.RUNNING) for _ in range(20)])

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, horloge, attente_max=10.0, pause=4.0)("SELECT 1", {})

    assert service.annulations == ["s1"]
    assert 10.0 <= horloge.instant - DEPART < 14.0


def test_une_requete_est_annulee_des_que_le_delai_est_atteint_et_non_une_pause_plus_tard():
    horloge = Horloge()
    service = FauxService(_reponse(StatementState.RUNNING), suivantes=[_reponse(StatementState.RUNNING) for _ in range(20)])

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, horloge, attente_max=4.0, pause=2.0)("SELECT 1", {})

    assert horloge.instant - DEPART == 4.0 and service.annulations == ["s1"]


class ServiceQuiGardeLaMain(FauxService):
    """Un envoi qui reste `duree` secondes sur place avant de rendre une requête encore en cours : l'attente synchrone (`wait_timeout`)."""

    def __init__(self, horloge, duree, premiere, suivantes=()):
        super().__init__(premiere, suivantes)
        self.horloge, self.duree = horloge, duree

    def execute_statement(self, **options):
        self.horloge.instant += self.duree
        return super().execute_statement(**options)


def test_le_delai_compte_depuis_l_envoi_et_non_depuis_la_fin_de_l_attente_synchrone():
    horloge = Horloge()
    en_cours = [_reponse(StatementState.RUNNING) for _ in range(60)]
    service = ServiceQuiGardeLaMain(horloge, 50.0, _reponse(StatementState.RUNNING), en_cours)

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, horloge, attente_max=60.0, pause=2.0)("SELECT 1", {})

    assert horloge.instant - DEPART == 60.0 and service.annulations == ["s1"]


def test_une_attente_synchrone_qui_use_tout_le_delai_annule_sans_attendre_davantage():
    horloge = Horloge()
    en_cours = [_reponse(StatementState.RUNNING) for _ in range(60)]
    service = ServiceQuiGardeLaMain(horloge, 50.0, _reponse(StatementState.RUNNING), en_cours)

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, horloge, attente_max=30.0, pause=2.0)("SELECT 1", {})

    assert horloge.pauses == [] and service.interrogations == [] and service.annulations == ["s1"]


def test_les_appels_au_service_respectent_les_signatures_du_vrai_sdk():
    """Le faux service des autres tests accepte n'importe quel mot-clé ; celui-ci a les signatures de la VRAIE classe (`create_autospec`) :
    un mot-clé qu'une version du SDK ne connaîtrait plus, ou une arité fausse, fait échouer l'envoi, l'attente ou la suite des morceaux."""
    service = create_autospec(StatementExecutionAPI, instance=True)
    service.execute_statement.return_value = _reponse(StatementState.PENDING)
    service.get_statement.side_effect = [
        _reponse(StatementState.RUNNING),
        _reponse(SUCCES, [_colonne("n", "LONG", "BIGINT")], [["1"], ["2"]], suivant=1),
    ]
    service.get_statement_result_chunk_n.return_value = ResultData(data_array=[["3"]], chunk_index=1, next_chunk_index=None)

    rendu = _executeur(service)("SELECT n FROM t WHERE x = :x", {"x": "1"})

    assert [ligne["n"] for ligne in rendu] == [1, 2, 3]


def test_l_annulation_respecte_la_signature_du_vrai_sdk():
    service = create_autospec(StatementExecutionAPI, instance=True)
    service.execute_statement.return_value = _reponse(StatementState.RUNNING)
    service.get_statement.return_value = _reponse(StatementState.RUNNING)

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, attente_max=5.0, pause=2.0)("SELECT 1", {})

    service.cancel_execution.assert_called_once_with("s1")


def test_une_annulation_qui_echoue_ne_masque_pas_l_erreur_de_delai():
    class AnnulationImpossible(FauxService):
        def cancel_execution(self, statement_id):
            raise RuntimeError("annulation refusée")

    horloge = Horloge()
    service = AnnulationImpossible(_reponse(StatementState.RUNNING), suivantes=[_reponse(StatementState.RUNNING) for _ in range(20)])

    with pytest.raises(ErreurService, match="délai"):
        _executeur(service, horloge, attente_max=3.0, pause=2.0)("SELECT 1", {})


# ==============================================================================
# 4. 🔗 AVEC L'ÉCRIVAIN : une table lue par l'exécuteur redevient les lignes d'origine
# ==============================================================================
@pytest.fixture(scope="module")
def journee_jouee(contrat, tmp_path_factory):
    catalogue = charger_catalogue(CHEMIN_CATALOGUE)
    journee = journee_multi_usines(contrat)
    resultats = jouer(contrat, catalogue, journee, tmp_path_factory.mktemp("jour-sdk"), Etat())
    return resultats


def _objets(nom, resultats):
    if nom == "registre":
        return [resultat.ligne_registre for resultat in resultats]
    extraire = {"bronze": lambda r: r.lignes_bronze, "coupees": lambda r: r.lignes_coupees, "silver": lambda r: r.silver.lignes,
                "quarantaine": lambda r: r.silver.quarantaine}[nom]
    return [objet for resultat in resultats for objet in extraire(resultat)]


def _texte_du_service(valeur):
    """Comment le service rend une valeur (hypothèse de ces tests, à confirmer sur un workspace) : du texte, `None` pour l'absence."""
    if valeur is None:
        return None
    if isinstance(valeur, datetime):
        return valeur.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    if isinstance(valeur, date):
        return valeur.isoformat()
    if isinstance(valeur, (list, dict)):
        return json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))
    return repr(valeur) if isinstance(valeur, float) else str(valeur)


def _colonnes_du_service(table):
    return [
        _colonne(colonne.nom, TYPES_DU_SERVICE.get(colonne.type_sql, colonne.type_sql.split("<")[0]), colonne.type_sql, position)
        for position, colonne in enumerate(table.colonnes)
    ]


@pytest.mark.parametrize("nom", ["registre", "bronze", "coupees", "silver", "quarantaine"])
def test_une_table_lue_par_le_magasin_sql_redevient_les_lignes_d_origine(contrat, journee_jouee, nom):
    couche = couches(contrat)[nom]
    objets = _objets(nom, journee_jouee)
    assert objets, f"la journée doit produire des lignes pour la couche {nom}"
    enregistrements = [couche.vers_enregistrement(objet) for objet in objets]
    lignes = [[_texte_du_service(e[colonne.nom]) for colonne in couche.table.colonnes] for e in enregistrements]
    service = FauxService(_reponse(SUCCES, _colonnes_du_service(couche.table), lignes))
    magasin = MagasinSql(_executeur(service), CATALOGUE_PAR_DEFAUT)

    lus = magasin.lire(couche.table, couche.ordre)

    assert lus == enregistrements
    assert [couche.depuis_enregistrement(e) for e in lus] == objets


def test_une_ecriture_du_magasin_sql_part_en_un_merge_avec_un_seul_parametre_json(contrat, journee_jouee):
    couche = couches(contrat)["registre"]
    enregistrements = [couche.vers_enregistrement(objet) for objet in _objets("registre", journee_jouee)]
    service = FauxService(_reponse(SUCCES, [_colonne("num_affected_rows", "LONG", "BIGINT")], [[str(len(enregistrements))]]))
    magasin = MagasinSql(_executeur(service), CATALOGUE_PAR_DEFAUT)

    magasin.inserer_absentes(couche.table, couche.cles, enregistrements)

    (envoi,) = service.envois
    assert envoi["statement"].startswith("MERGE INTO") and [p.name for p in envoi["parameters"]] == ["lignes"]
    assert len(json.loads(envoi["parameters"][0].value)) == len(enregistrements)
