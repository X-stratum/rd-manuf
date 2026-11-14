"""Une JOURNÉE MULTI-USINES : des livraisons de trois pays, jouées dans l'ordre d'arrivée sur le vrai enchaînement, l'état s'accumulant.

Chaque issue d'une livraison (acceptée, avec rejets, bloquée puis renvoyée, refusée, rejouée, en conflit) est déjà testée seule. Ce module
joue ce qu'une livraison seule ne montre pas : l'ÉTAT CUMULÉ d'une journée, des INVARIANTS sur toute la journée, et la journée REJOUÉE le
lendemain sur l'état déjà cumulé. Il est écrit pour être rejoué tel quel par le futur test fonctionnel de l'écrivain Spark et par les tests
de volume : seul `Etat` sera remplacé (des listes aujourd'hui, des tables demain).

L'ATTENDU de chaque livraison est écrit ici d'après sa RECETTE (« 2 lignes hors plage sur 100 »), jamais lu dans un résultat du pipeline :
c'est ce qui permet de comparer le pipeline à quelque chose. Les nombres de rejets viennent du seuil du contrat : la journée suit le contrat
au lieu d'en recopier un nombre. Tous les fichiers sont fabriqués par le simulateur ; l'ordre d'arrivée compte (la première arrivée fait foi
pour un conflit de clé) et l'heure vient de la journée, jamais de l'horloge.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rd_manuf.alertes import CODE_LIVRAISON_BLOQUEE, Alerte, Catalogue
from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee
from rd_manuf.contrat import CodeErreur, Contrat, StatutLivraison
from rd_manuf.pipeline import ResultatLivraison, traiter_livraison
from rd_manuf.registre import STATUT_REJEU, STATUTS_INTEGRES, LigneRegistre
from rd_manuf.silver import LigneQuarantaine, LigneSilver
from rd_manuf.simulateur import (
    DefautDeFichier,
    ecrire_livraison_csv,
    ecrire_livraison_parquet,
    generer_livraison,
    nom_de_fichier,
)
from rd_manuf.structure import CodeStructure

# ==============================================================================
# ⚙️ PARAMÈTRES DE LA JOURNÉE
# ==============================================================================
NB_LIGNES = 100                                         # lignes d'une livraison : 100 laisse une précision d'un pour cent au seuil
PREMIERE_ARRIVEE = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
PAS_ENTRE_ARRIVEES = timedelta(minutes=15)
DELAI_FICHIER_ARRIVEE = timedelta(minutes=10)           # l'instant écrit dans un nom précède l'arrivée de la livraison
UN_JOUR = timedelta(days=1)                             # le rejeu de la journée arrive le lendemain
NOM_INVALIDE = "livraison_production_FRA_AA_001.csv"    # il manque l'instant : la convention de `nom_fichier.py` le refuse

D = DefautDeFichier


# ==============================================================================
# 📋 L'ATTENDU ET LA LIVRAISON
# ==============================================================================
@dataclass(frozen=True)
class Attendu:
    """Ce qu'une livraison doit produire, couche par couche : écrit d'après la recette, jamais déduit d'une exécution."""

    statut: str                         # le statut de sa ligne de registre, ou `STATUT_REJEU`
    alertes: tuple[str, ...]            # les codes d'alerte, dans l'ordre où le pipeline les rend
    nb_bronze: int                      # lignes bien formées gardées au bronze
    nb_coupees: int                     # lignes coupées gardées à part au bronze
    nb_silver: int
    nb_quarantaine: int

    @classmethod
    def observe(cls, resultat: ResultatLivraison) -> Attendu:
        """Ce que le pipeline a réellement produit, dans la même forme, pour le comparer à ce qui était attendu."""
        return cls(
            statut=resultat.ligne_registre.statut,
            alertes=tuple(alerte.code for alerte in resultat.alertes),
            nb_bronze=len(resultat.lignes_bronze),
            nb_coupees=len(resultat.lignes_coupees),
            nb_silver=len(resultat.silver.lignes),
            nb_quarantaine=len(resultat.silver.quarantaine),
        )


@dataclass(frozen=True)
class Livraison:
    etiquette: str                                  # un nom court, stable, pour les tests et les messages
    nom: str                                        # le nom du fichier (celui du registre) ; deux livraisons peuvent en partager un
    arrivee: datetime                               # en UTC, avec fuseau
    ecrire: Callable[[Contrat, Path], None]         # fabrique le fichier à ce chemin, au moment où la livraison arrive
    attendu: Attendu
    attendu_au_rejeu: Attendu                       # ce que la même livraison, revenue le lendemain, doit produire


@dataclass
class Etat:
    """Tout ce que la journée a produit, couche par couche : ce que l'écrivain Spark écrira dans des tables."""

    registre: list[LigneRegistre] = field(default_factory=list)
    lignes_bronze: list[LigneBronze] = field(default_factory=list)
    lignes_coupees: list[LigneBronzeCoupee] = field(default_factory=list)
    lignes_silver: list[LigneSilver] = field(default_factory=list)
    quarantaine: list[LigneQuarantaine] = field(default_factory=list)
    alertes: list[Alerte] = field(default_factory=list)

    def appliquer(self, resultat: ResultatLivraison) -> None:
        """Ajoute ce qu'une livraison a produit. Les listes du résultat ne sont pas gardées : l'état ne partage rien avec lui."""
        self.registre.append(resultat.ligne_registre)
        self.lignes_bronze.extend(resultat.lignes_bronze)
        self.lignes_coupees.extend(resultat.lignes_coupees)
        self.lignes_silver.extend(resultat.silver.lignes)
        self.quarantaine.extend(resultat.silver.quarantaine)
        self.alertes.extend(resultat.alertes)


# ==============================================================================
# 🧮 LES NOMBRES DE LA JOURNÉE, dérivés du seuil de rejet du contrat
# ==============================================================================
def _rejets_tolerables(contrat: Contrat) -> int:
    """Le plus grand nombre de rejets, sur `NB_LIGNES`, qui laisse la livraison passer : le seuil ne bloque qu'au-dessus."""
    nombre = math.floor(round(contrat.taux_rejet_max * NB_LIGNES, 9))     # l'arrondi évite 28,999999 pour 29
    if nombre < 1:
        raise ValueError(
            f"Le seuil de rejet du contrat ({contrat.taux_rejet_max:.1%}) ne laisse passer aucun rejet sur {NB_LIGNES} lignes : "
            f"la journée n'a pas de livraison « avec rejets »."
        )
    return nombre


# ==============================================================================
# 🧾 LES ATTENDUS : un par issue, écrits d'après la recette
# ==============================================================================
def _acceptee(nb: int = NB_LIGNES) -> Attendu:
    return Attendu("ACCEPTEE", (), nb, 0, nb, 0)


def _avec_rejets(rejets: int) -> Attendu:
    return Attendu("ACCEPTEE_AVEC_REJETS", (CodeErreur.HORS_PLAGE.value,), NB_LIGNES, 0, NB_LIGNES - rejets, rejets)


def _bloquee() -> Attendu:
    return Attendu("BLOQUEE", (CODE_LIVRAISON_BLOQUEE, CodeErreur.HORS_PLAGE.value), NB_LIGNES, 0, 0, 0)


def _tout_en_conflit_de_cle() -> Attendu:
    """Acceptée (les conflits de clé entre livraisons ne changent pas le statut), mais aucune ligne n'entre au silver."""
    return Attendu("ACCEPTEE", (CodeErreur.CLE_EN_CONFLIT.value,), NB_LIGNES, 0, 0, NB_LIGNES)


def _lignes_coupees(coupees: int) -> Attendu:
    bien_formees = NB_LIGNES - coupees
    return Attendu("ACCEPTEE_AVEC_REJETS", (CodeErreur.LIGNE_MALFORMEE.value,), bien_formees, coupees, bien_formees, coupees)


def _refusee(code: CodeStructure) -> Attendu:
    return Attendu(StatutLivraison.REFUSEE.value, (code.value,), 0, 0, 0, 0)


REJEU = Attendu(STATUT_REJEU, (), 0, 0, 0, 0)


# ==============================================================================
# 🛠️ LES RECETTES : chacune fabrique un fichier avec le simulateur
# ==============================================================================
def _csv(graine: int, premier_lot: int = 1, anomalies: dict[CodeErreur, int] | None = None, defauts: dict[D, int] | None = None):
    def ecrire(contrat: Contrat, chemin: Path) -> None:
        lignes = generer_livraison(contrat, NB_LIGNES, graine, anomalies, premier_lot)
        ecrire_livraison_csv(contrat, lignes, chemin, defauts=defauts)

    return ecrire


def _parquet(graine: int, premier_lot: int = 1, defauts: dict[D, int] | None = None):
    def ecrire(contrat: Contrat, chemin: Path) -> None:
        ecrire_livraison_parquet(contrat, generer_livraison(contrat, NB_LIGNES, graine, premier_lot=premier_lot), chemin, defauts=defauts)

    return ecrire


@dataclass(frozen=True)
class _Entree:
    """Une ligne du programme de la journée, avant qu'on lui donne son heure et son nom."""

    etiquette: str
    origine: tuple[str, str, str] | None    # (pays, usine, ligne) ; `None` pour un nom invalide, écrit tel quel
    format: str
    ecrire: Callable[[Contrat, Path], None]
    attendu: Attendu
    attendu_au_rejeu: Attendu
    meme_nom_que: str | None = None         # l'étiquette de la livraison dont celle-ci reprend le nom (renvoi, rejeu, conflit)


def _programme(contrat: Contrat) -> list[_Entree]:
    rejets = _rejets_tolerables(contrat)
    seconde_plage = NB_LIGNES + 1                       # les lots 101 à 200 : une autre ligne d'une même usine, sans recouvrir la première
    conflit_de_livraison = _refusee(CodeStructure.LIVRAISON_EN_CONFLIT)
    return [
        _Entree("fra-aa-001-saine", ("FRA", "AA", "001"), "csv", _csv(1), _acceptee(), REJEU),
        _Entree("fra-aa-002-parquet-saine", ("FRA", "AA", "002"), "parquet", _parquet(2, seconde_plage), _acceptee(), REJEU),
        # Mêmes numéros de lot que FRA_AA_001, mais une autre usine : aucun conflit, un lot s'identifie par son pays et son usine.
        _Entree("deu-ba-001-avec-rejets", ("DEU", "BA", "001"), "csv", _csv(3, anomalies={CodeErreur.HORS_PLAGE: rejets}),
                _avec_rejets(rejets), REJEU),
        _Entree("deu-ba-002-parquet-saine", ("DEU", "BA", "002"), "parquet", _parquet(4, seconde_plage), _acceptee(), REJEU),
        # Un rejet de plus que le seuil ne tolère : bloquée. Le renvoi corrigé reprend le nom. Rejouée plus tard, la première version
        # n'est plus un rejeu mais un conflit : le même nom est intégré avec un autre contenu.
        _Entree("usa-ca-001-bloquee", ("USA", "CA", "001"), "csv", _csv(5, anomalies={CodeErreur.HORS_PLAGE: rejets + 1}),
                _bloquee(), conflit_de_livraison),
        _Entree("usa-ca-001-renvoyee-corrigee", ("USA", "CA", "001"), "csv", _csv(5), _acceptee(), REJEU,
                meme_nom_que="usa-ca-001-bloquee"),
        _Entree("fra-aa-001-rejeu", ("FRA", "AA", "001"), "csv", _csv(1), REJEU, REJEU, meme_nom_que="fra-aa-001-saine"),
        _Entree("fra-aa-001-conflit-de-livraison", ("FRA", "AA", "001"), "csv", _csv(8), conflit_de_livraison, conflit_de_livraison,
                meme_nom_que="fra-aa-001-saine"),
        # Une autre ligne de la même usine reprend les numéros 1 à 100 avec un autre contenu : la première arrivée fait foi.
        _Entree("fra-aa-003-conflit-de-cle", ("FRA", "AA", "003"), "csv", _csv(9), _tout_en_conflit_de_cle(), REJEU),
        _Entree("nom-invalide", None, "csv", _csv(10), _refusee(CodeStructure.NOM_FICHIER_INVALIDE),
                _refusee(CodeStructure.NOM_FICHIER_INVALIDE)),
        _Entree("fra-aa-004-colonne-manquante", ("FRA", "AA", "004"), "csv", _csv(11, defauts={D.COLONNE_MANQUANTE: 1}),
                _refusee(CodeStructure.COLONNE_MANQUANTE), _refusee(CodeStructure.COLONNE_MANQUANTE)),
        _Entree("fra-aa-005-fichier-vide", ("FRA", "AA", "005"), "csv", _csv(12, defauts={D.FICHIER_VIDE: 1}),
                _refusee(CodeStructure.FICHIER_VIDE), _refusee(CodeStructure.FICHIER_VIDE)),
        _Entree("deu-ba-003-fichier-illisible", ("DEU", "BA", "003"), "csv", _csv(13, defauts={D.FICHIER_ILLISIBLE: 1}),
                _refusee(CodeStructure.FICHIER_ILLISIBLE), _refusee(CodeStructure.FICHIER_ILLISIBLE)),
        _Entree("deu-ba-004-parquet-colonne-inconnue", ("DEU", "BA", "004"), "parquet", _parquet(14, defauts={D.COLONNE_INCONNUE: 1}),
                _refusee(CodeStructure.COLONNE_INCONNUE), _refusee(CodeStructure.COLONNE_INCONNUE)),
        _Entree("deu-ba-005-parquet-colonne-dupliquee", ("DEU", "BA", "005"), "parquet", _parquet(15, defauts={D.COLONNE_DUPLIQUEE: 1}),
                _refusee(CodeStructure.COLONNE_DUPLIQUEE), _refusee(CodeStructure.COLONNE_DUPLIQUEE)),
        # Une ligne coupée en route, que le seuil tolère : le fichier entre, la ligne coupée est gardée à part et mise en quarantaine.
        _Entree("usa-ca-002-lignes-coupees", ("USA", "CA", "002"), "csv", _csv(16, seconde_plage, defauts={D.LIGNE_COUPEE: 1}),
                _lignes_coupees(1), REJEU),
        # Une autre usine du MÊME pays reprend aussi les numéros 1 à 100 : l'usine fait partie de l'identité d'un lot, pas que le pays.
        _Entree("usa-cb-001-memes-numeros-autre-usine", ("USA", "CB", "001"), "csv", _csv(17), _acceptee(), REJEU),
    ]


def journee_multi_usines(contrat: Contrat) -> list[Livraison]:
    """La journée du 2026-11-12 (UTC), dans l'ordre d'arrivée : une livraison par issue.

    Les arrivées sont espacées de quinze minutes ; l'instant écrit dans chaque nom précède l'arrivée de dix minutes, sauf pour une
    livraison qui reprend le nom d'une autre (renvoi corrigé, rejeu, conflit), qui reprend aussi son instant.
    Refuse un contrat dont le seuil de rejet ne tolère aucun rejet sur `NB_LIGNES` lignes.
    """
    noms: dict[str, str] = {}
    livraisons = []
    for rang, entree in enumerate(_programme(contrat)):
        arrivee = PREMIERE_ARRIVEE + rang * PAS_ENTRE_ARRIVEES
        if entree.meme_nom_que is not None:
            nom = noms[entree.meme_nom_que]
        elif entree.origine is None:
            nom = NOM_INVALIDE
        else:
            pays, usine, ligne = entree.origine
            nom = nom_de_fichier(pays, usine, ligne, arrivee - DELAI_FICHIER_ARRIVEE, entree.format)
        noms[entree.etiquette] = nom
        livraisons.append(Livraison(entree.etiquette, nom, arrivee, entree.ecrire, entree.attendu, entree.attendu_au_rejeu))
    return livraisons


# ==============================================================================
# ▶️ JOUER
# ==============================================================================
def jouer(
    contrat: Contrat,
    catalogue: Catalogue,
    livraisons: Sequence[Livraison],
    dossier: Path,
    etat: Etat,
    decalage: timedelta = timedelta(0),
) -> list[ResultatLivraison]:
    """Fait arriver les livraisons dans l'ordre : chaque fichier est fabriqué au moment de son arrivée, traité, puis ajouté à `etat`.

    `etat` est MODIFIÉ : c'est lui qui garde la journée. Le `decalage` repousse toutes les arrivées (un rejeu le lendemain). Les fichiers
    sont écrits dans `dossier` ; deux livraisons de même nom s'y remplacent, comme un renvoi remplace le fichier.
    """
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    resultats = []
    for livraison in livraisons:
        chemin = dossier / livraison.nom
        livraison.ecrire(contrat, chemin)
        resultat = traiter_livraison(contrat, catalogue, chemin, etat.registre, etat.lignes_silver, livraison.arrivee + decalage)
        etat.appliquer(resultat)
        resultats.append(resultat)
    return resultats


# ==============================================================================
# 📐 LES INVARIANTS : ce qui doit rester vrai de toute journée, quelles que soient ses livraisons
# ==============================================================================
def verifier_invariants(
    contrat: Contrat, catalogue: Catalogue, etat: Etat, resultats: Sequence[ResultatLivraison]
) -> list[str]:
    """Rend les violations, une phrase chacune (une liste vide : tout est vrai). Ne modifie ni l'état ni les résultats.

    - registre : une ligne par livraison reçue ;
    - doublon : un lot (pays, usine, clé) n'est jamais deux fois au silver ;
    - conservation : une livraison qui entre garde au bronze tout ce qui est arrivé, et ce qui est au silver ou en quarantaine est
      exactement ce qui est au bronze (valable pour une journée sans doublon exact) ; une livraison refusée ou rejouée n'a rien ;
      une livraison bloquée garde son bronze et n'a rien au silver ;
    - alertes : aucune pour un rejeu ni pour une livraison saine, et tout code est connu du catalogue.
    """
    violations: list[str] = []
    if len(etat.registre) != len(resultats):
        violations.append(f"registre : {len(etat.registre)} lignes pour {len(resultats)} livraisons reçues.")

    vus: dict[tuple, list[str]] = defaultdict(list)
    for ligne in etat.lignes_silver:
        cle = tuple(str(ligne.valeurs[nom]) for nom in contrat.cle_primaire)
        vus[(ligne.pays, ligne.usine, cle)].append(ligne.livraison_id)
    for lot, livraisons in vus.items():
        if len(livraisons) > 1:
            violations.append(f"doublon : le lot {lot} est {len(livraisons)} fois au silver (livraisons {', '.join(livraisons)}).")

    def compter(lignes, ligne_registre) -> int:
        cle = (ligne_registre.livraison_id, ligne_registre.heure_arrivee)
        return sum(1 for ligne in lignes if (ligne.livraison_id, ligne.heure_arrivee) == cle)

    for ligne in etat.registre:
        bronze, coupees = compter(etat.lignes_bronze, ligne), compter(etat.lignes_coupees, ligne)
        silver, quarantaine = compter(etat.lignes_silver, ligne), compter(etat.quarantaine, ligne)
        nom = f"{ligne.livraison_id} ({ligne.statut}, arrivée {ligne.heure_arrivee:%H:%M})"
        if ligne.statut in STATUTS_BRONZE:
            if bronze + coupees != ligne.nb_recues:
                violations.append(f"conservation : {nom} a reçu {ligne.nb_recues} lignes mais en garde {bronze + coupees} au bronze.")
        elif bronze or coupees:
            violations.append(f"conservation : {nom} n'entre pas, mais a {bronze + coupees} lignes au bronze.")
        if ligne.statut in STATUTS_INTEGRES:
            if silver + quarantaine != bronze + coupees:
                violations.append(
                    f"conservation : {nom} a {bronze + coupees} lignes au bronze mais {silver} au silver et {quarantaine} en quarantaine."
                )
        elif silver or quarantaine:
            violations.append(f"silver : {nom} n'est pas intégrée, mais a {silver} lignes au silver et {quarantaine} en quarantaine.")

    codes_connus = set(catalogue.regles) | set(catalogue.regles_structure) | {CODE_LIVRAISON_BLOQUEE}
    for resultat in resultats:
        ligne = resultat.ligne_registre
        if ligne.statut == STATUT_REJEU and resultat.alertes:
            violations.append(f"alertes : le rejeu {ligne.livraison_id} ne devrait prévenir personne.")
        saine = ligne.statut == StatutLivraison.ACCEPTEE.value and not resultat.silver.quarantaine and not resultat.lignes_coupees
        if saine and resultat.alertes:
            violations.append(f"alertes : la livraison saine {ligne.livraison_id} ne devrait prévenir personne.")
        for alerte in resultat.alertes:
            if alerte.code not in codes_connus:
                violations.append(f"alertes : le code {alerte.code} de {ligne.livraison_id} est absent du catalogue.")
    return violations
