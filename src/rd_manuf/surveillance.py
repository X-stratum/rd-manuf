"""Surveillance, premier axe : la FRAÎCHEUR des livraisons.

Le pipeline ne réagit qu'aux fichiers qui ARRIVENT : une source qui n'envoie plus rien ne déclenche aucune alerte. On déclare donc les
sources attendues et le délai maximal entre deux livraisons (`contracts/surveillance/sources_attendues.yaml`), et `controler_fraicheur`
compare le registre à « maintenant » pour rendre des CONSTATS :

- `AUCUNE_LIVRAISON_INTEGREE` : une source déclarée n'a jamais eu de livraison intégrée (elle a peut-être livré, mais refusé ou bloqué :
  le constat dit le dernier statut reçu) ;
- `LIVRAISON_EN_RETARD` : sa dernière livraison intégrée a PLUS (strictement) que le délai maximal ;
- `SOURCE_INCONNUE` : une origine livre sans être déclarée.

Seule une livraison INTÉGRÉE (acceptée, ou acceptée avec rejets) rafraîchit une source : une livraison bloquée, refusée ou rejouée n'a rien
apporté au silver. Une arrivée dans le futur (horloge décalée) n'est pas un retard. Pas de calendrier : une usine arrêtée le week-end
demande un délai qui le couvre.

Second axe : le TAUX DE REJET dans le temps, par source (`controler_taux_de_rejet`). Le seuil n'est pas inventé ici : c'est celui du
contrat de données (`taux_rejet_max`), au-dessus duquel une livraison est bloquée. Le niveau de PRÉ-ALERTE en est une part
(`part_du_seuil`), réglée avec la fenêtre de livraisons (`fenetre_livraisons`) dans la section `taux_de_rejet` du fichier de
surveillance : si le contrat change son seuil, le niveau suit. Le taux d'une source est celui des SOMMES (rejetés sur reçus, sur ses N
dernières livraisons jugeables), jamais la moyenne des taux des livraisons ; une livraison bloquée compte (c'est le signal), une
refusée ou rejouée n'a pas de taux et ne compte pas. Une source qui a moins de N livraisons jugeables n'est pas jugée : une pré-alerte
sur une seule livraison serait du bruit (l'alerte de blocage existe déjà).

Ce sont des fonctions PURES : elles ne lisent que ce qu'on leur donne (« maintenant » est un paramètre, avec fuseau), n'écrivent rien et ne
routent rien vers des personnes (annuaire et envoi viendront ailleurs).
"""
from __future__ import annotations

import math
import re
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from rd_manuf.contrat import StatutLivraison
from rd_manuf.nom_fichier import MOTIF_LIGNE, MOTIF_PAYS, MOTIF_USINE
from rd_manuf.registre import STATUTS_INTEGRES, LigneRegistre

AUCUNE_LIVRAISON_INTEGREE = "AUCUNE_LIVRAISON_INTEGREE"
LIVRAISON_EN_RETARD = "LIVRAISON_EN_RETARD"
SOURCE_INCONNUE = "SOURCE_INCONNUE"
TAUX_DE_REJET_ELEVE = "TAUX_DE_REJET_ELEVE"

CHAMPS_DE_LA_SOURCE = ("pays", "usine", "ligne", "delai_max_heures")
CHAMPS_DU_FICHIER = ("version", "sources", "taux_de_rejet")
CHAMPS_DU_TAUX_DE_REJET = ("fenetre_livraisons", "part_du_seuil")
MOTIFS = {"pays": MOTIF_PAYS, "usine": MOTIF_USINE, "ligne": MOTIF_LIGNE}      # la convention du nom de fichier, écrite à un seul endroit

Origine = tuple[str, str, str]                                                  # (pays, usine, ligne de production)


class SourcesInvalides(ValueError):
    """Le fichier des sources attendues est mal formé : le message dit quelle source (son rang) ou quel champ est en cause."""


@dataclass(frozen=True)
class SourceAttendue:
    pays: str
    usine: str
    ligne: str                                  # la ligne de production de l'usine (001), comme dans le nom du fichier
    delai_max: timedelta                        # au-delà (strictement) de ce délai depuis la dernière livraison intégrée : en retard


@dataclass(frozen=True)
class Constat:
    code: str
    pays: str
    usine: str
    ligne: str
    derniere_arrivee: datetime | None           # en retard : la dernière intégrée ; inconnue : la dernière, tout statut
    age: timedelta | None                       # « maintenant » moins `derniere_arrivee` ; vide s'il n'y a pas de livraison à mesurer
    delai_max: timedelta | None                 # vide pour une source inconnue : personne n'a décidé de son rythme
    dernier_statut: str | None                  # le statut de la livraison la plus récente de l'origine, quel qu'il soit ; vide si aucune


def _probleme_fenetre(valeur: Any) -> str | None:
    if isinstance(valeur, bool) or not isinstance(valeur, int) or valeur < 1:
        return "« fenetre_livraisons » doit être un entier d'au moins 1."
    return None


def _probleme_part(valeur: Any) -> str | None:
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not math.isfinite(valeur) or not 0 < valeur <= 1:
        return "« part_du_seuil » doit être un nombre fini, supérieur à 0 et au plus 1."
    return None


@dataclass(frozen=True)
class ParametresTauxRejet:
    fenetre_livraisons: int                     # combien des dernières livraisons jugeables d'une source on regarde
    part_du_seuil: float                        # niveau de pré-alerte : cette part du seuil de rejet du contrat (0,5 : la moitié)

    def __post_init__(self) -> None:
        for probleme in (_probleme_fenetre(self.fenetre_livraisons), _probleme_part(self.part_du_seuil)):
            if probleme is not None:
                raise ValueError(probleme)


@dataclass(frozen=True)
class ConstatTauxRejet:
    code: str
    pays: str
    usine: str
    ligne: str
    taux: float                                 # rejetés sur reçus, sur les `nb_livraisons` dernières livraisons jugeables
    niveau: float                               # le niveau d'alerte : `part_du_seuil` × le seuil du contrat
    seuil_contrat: float                        # le seuil de rejet du contrat, au-dessus duquel une livraison est bloquée
    nb_livraisons: int
    nb_bloquees: int                            # parmi elles, combien ont été bloquées


# ==============================================================================
# 📥 LE FICHIER DES SOURCES ATTENDUES
# ==============================================================================
class _LecteurSansCleRepetee(yaml.SafeLoader):
    """PyYAML garde en silence la DERNIÈRE valeur d'une clé répétée : une copie-colle ferait disparaître des sources de la surveillance,
    ou changerait un délai, sans un mot. Ce lecteur refuse la répétition, avec le nom de la clé et la ligne du fichier."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        vues: set[Any] = set()
        for noeud_cle, _ in node.value:
            if noeud_cle.tag == "tag:yaml.org,2002:merge":                 # la fusion `<<` a sa propre règle : ses clés se remplacent
                continue
            cle = self.construct_object(noeud_cle, deep=True)
            if isinstance(cle, Hashable):
                if cle in vues:
                    raise yaml.constructor.ConstructorError(None, None, f"la clé « {cle} » est répétée", noeud_cle.start_mark)
                vues.add(cle)
        return super().construct_mapping(node, deep)


def _lire_yaml(chemin: str | Path) -> Any:
    try:
        return yaml.load(Path(chemin).read_text(encoding="utf-8"), Loader=_LecteurSansCleRepetee)
    except yaml.YAMLError as erreur:
        raise SourcesInvalides(f"Le fichier des sources attendues n'est pas du YAML lisible : {erreur}") from erreur


def _valider_texte(rang: int, champ: str, valeur: Any) -> str:
    if isinstance(valeur, int) and not isinstance(valeur, bool):
        raise SourcesInvalides(
            f"source {rang} : « {champ} » vaut {valeur!r}, un nombre : l'écrire entre guillemets (YAML lit 001 comme le nombre 1)."
        )
    if not isinstance(valeur, str) or re.fullmatch(MOTIFS[champ], valeur) is None:
        raise SourcesInvalides(f"source {rang} : « {champ} » doit suivre la convention du nom de fichier (motif {MOTIFS[champ]}).")
    return valeur


def _valider_delai(rang: int, valeur: Any) -> timedelta:
    message = f"source {rang} : « delai_max_heures » doit être un nombre d'heures fini et positif."
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or not math.isfinite(valeur) or valeur <= 0:
        raise SourcesInvalides(message)
    try:
        duree = timedelta(hours=valeur)
    except OverflowError:
        raise SourcesInvalides(f"source {rang} : « delai_max_heures » est trop grand.") from None
    if duree <= timedelta(0):                  # un nombre positif mais si petit que la durée, arrondie à la microseconde, est nulle
        raise SourcesInvalides(f"source {rang} : « delai_max_heures » est si petit que la durée obtenue est nulle.")
    return duree


def _valider_source(rang: int, brut: Any) -> SourceAttendue:
    if not isinstance(brut, dict):
        raise SourcesInvalides(f"source {rang} : un dictionnaire de champs est attendu ({', '.join(CHAMPS_DE_LA_SOURCE)}).")
    for champ in CHAMPS_DE_LA_SOURCE:
        if champ not in brut:
            raise SourcesInvalides(f"source {rang} : le champ « {champ} » manque.")
    for champ in brut:
        if champ not in CHAMPS_DE_LA_SOURCE:
            raise SourcesInvalides(f"source {rang} : champ inconnu « {champ} » (attendus : {', '.join(CHAMPS_DE_LA_SOURCE)}).")
    return SourceAttendue(
        pays=_valider_texte(rang, "pays", brut["pays"]),
        usine=_valider_texte(rang, "usine", brut["usine"]),
        ligne=_valider_texte(rang, "ligne", brut["ligne"]),
        delai_max=_valider_delai(rang, brut["delai_max_heures"]),
    )


def _lire_fichier(chemin: str | Path) -> dict[str, Any]:
    """Le fichier de surveillance lu : un dictionnaire dont les clés sont toutes connues (une faute de frappe ne passe pas)."""
    brut = _lire_yaml(chemin)
    if not isinstance(brut, dict):
        raise SourcesInvalides(f"Le fichier de surveillance doit être un dictionnaire ({', '.join(CHAMPS_DU_FICHIER)}).")
    for cle in brut:
        if cle not in CHAMPS_DU_FICHIER:
            raise SourcesInvalides(f"Clé inconnue « {cle} » (attendues : {', '.join(CHAMPS_DU_FICHIER)}).")
    return brut


def charger_parametres_taux_rejet(chemin: str | Path) -> ParametresTauxRejet:
    """La section `taux_de_rejet` du fichier de surveillance : la fenêtre de livraisons et la part du seuil du contrat. `SourcesInvalides`
    si la section manque, est mal formée, a un champ manquant, inconnu ou répété, ou une valeur hors de ce qui est permis."""
    section = _lire_fichier(chemin).get("taux_de_rejet")
    if not isinstance(section, dict) or not section:
        raise SourcesInvalides(f"La section « taux_de_rejet » manque ou est vide (champs attendus : {', '.join(CHAMPS_DU_TAUX_DE_REJET)}).")
    for champ in CHAMPS_DU_TAUX_DE_REJET:
        if champ not in section:
            raise SourcesInvalides(f"taux_de_rejet : le champ « {champ} » manque.")
    for champ in section:
        if champ not in CHAMPS_DU_TAUX_DE_REJET:
            raise SourcesInvalides(f"taux_de_rejet : champ inconnu « {champ} » (attendus : {', '.join(CHAMPS_DU_TAUX_DE_REJET)}).")
    for probleme in (_probleme_fenetre(section["fenetre_livraisons"]), _probleme_part(section["part_du_seuil"])):
        if probleme is not None:
            raise SourcesInvalides(f"taux_de_rejet : {probleme}")
    return ParametresTauxRejet(fenetre_livraisons=section["fenetre_livraisons"], part_du_seuil=float(section["part_du_seuil"]))


def charger_sources_attendues(chemin: str | Path) -> tuple[SourceAttendue, ...]:
    """Les sources déclarées, triées par pays, usine et ligne. Un fichier absent lève l'erreur de fichier habituelle ; un contenu mal formé,
    `SourcesInvalides`. Refuse tout ce qui est ambigu : champ manquant ou inconnu, doublon, délai nul ou non fini."""
    brut = _lire_fichier(chemin)
    liste = brut.get("sources")
    if not isinstance(liste, list) or not liste:
        raise SourcesInvalides("« sources » doit être une liste d'au moins une source.")
    sources: dict[Origine, SourceAttendue] = {}
    for rang, source_brute in enumerate(liste, start=1):
        source = _valider_source(rang, source_brute)
        origine = (source.pays, source.usine, source.ligne)
        if origine in sources:
            raise SourcesInvalides(f"source {rang} : l'origine {'_'.join(origine)} est déclarée deux fois.")
        sources[origine] = source
    return tuple(sources[origine] for origine in sorted(sources))


# ==============================================================================
# ⏱️ LA FRAÎCHEUR
# ==============================================================================
def _avec_fuseau(instant: datetime, description: str) -> None:
    if instant.utcoffset() is None:
        raise ValueError(f"{description} doit avoir un fuseau horaire (UTC de préférence) : un instant sans fuseau est ambigu.")


def _par_origine(registre: Iterable[LigneRegistre]) -> dict[Origine, tuple[LigneRegistre, LigneRegistre | None]]:
    """Pour chaque origine : sa livraison la plus récente (à arrivée égale, la dernière du registre), et sa dernière livraison INTÉGRÉE."""
    derniere: dict[Origine, LigneRegistre] = {}
    integree: dict[Origine, LigneRegistre] = {}
    for ligne in registre:
        if ligne.pays is None or ligne.usine is None or ligne.ligne is None:       # nom de fichier invalide : aucune origine
            continue
        _avec_fuseau(ligne.heure_arrivee, f"L'arrivée de la livraison {ligne.livraison_id}")
        origine = (ligne.pays, ligne.usine, ligne.ligne)
        if origine not in derniere or ligne.heure_arrivee >= derniere[origine].heure_arrivee:
            derniere[origine] = ligne
        if ligne.statut in STATUTS_INTEGRES and (origine not in integree or ligne.heure_arrivee >= integree[origine].heure_arrivee):
            integree[origine] = ligne
    return {origine: (ligne, integree.get(origine)) for origine, ligne in derniere.items()}


def controler_fraicheur(registre: Iterable[LigneRegistre], sources: Iterable[SourceAttendue], maintenant: datetime) -> list[Constat]:
    """Les constats de fraîcheur, triés par pays, usine, ligne puis code ; une liste vide : tout est frais. Ne modifie pas ses entrées."""
    _avec_fuseau(maintenant, "« maintenant »")
    vues = _par_origine(registre)
    declarees: dict[Origine, SourceAttendue] = {}
    for source in sources:
        origine = (source.pays, source.usine, source.ligne)
        if origine in declarees:
            raise ValueError(f"La source {'_'.join(origine)} est déclarée deux fois.")
        declarees[origine] = source

    constats = []
    for origine, source in declarees.items():
        derniere, integree = vues.get(origine, (None, None))
        dernier_statut = derniere.statut if derniere is not None else None
        if integree is None:
            constats.append(Constat(AUCUNE_LIVRAISON_INTEGREE, *origine, None, None, source.delai_max, dernier_statut))
            continue
        age = maintenant - integree.heure_arrivee
        if age > source.delai_max:
            constats.append(Constat(LIVRAISON_EN_RETARD, *origine, integree.heure_arrivee, age, source.delai_max, dernier_statut))
    for origine, (derniere, _) in vues.items():
        if origine not in declarees:
            age = max(timedelta(0), maintenant - derniere.heure_arrivee)         # une arrivée dans le futur (horloge décalée) a un âge nul
            constats.append(Constat(SOURCE_INCONNUE, *origine, derniere.heure_arrivee, age, None, derniere.statut))
    return sorted(constats, key=lambda c: (c.pays, c.usine, c.ligne, c.code))


# ==============================================================================
# 📉 LE TAUX DE REJET
# ==============================================================================
STATUTS_AVEC_UN_TAUX = frozenset({*STATUTS_INTEGRES, StatutLivraison.BLOQUEE.value})     # les livraisons dont des lignes ont été jugées


def _verifier_le_seuil(seuil: Any) -> float:
    if isinstance(seuil, bool) or not isinstance(seuil, (int, float)) or not 0 <= seuil <= 1:
        raise ValueError(f"taux_rejet_max doit être un nombre entre 0 et 1, comme dans le contrat (reçu {seuil!r}).")
    return float(seuil)


def controler_taux_de_rejet(
    registre: Iterable[LigneRegistre], parametres: ParametresTauxRejet, taux_rejet_max: float
) -> list[ConstatTauxRejet]:
    """Les sources dont le taux de rejet, sur leurs `fenetre_livraisons` dernières livraisons jugeables, dépasse (strictement)
    `part_du_seuil` fois le seuil du contrat. Triés par pays, usine, ligne. Ne modifie pas ses entrées.

    Jugeables : acceptée, acceptée avec rejets et BLOQUÉE (le blocage est le signal), avec au moins une ligne reçue. Une livraison
    refusée (sa structure est invalide, aucune ligne n'est jugée) ou rejouée n'a pas de taux : elle ne compte pas, même si le registre a
    gardé un nombre de lignes reçues. Le taux est celui des sommes. Une source qui a moins de livraisons jugeables que la fenêtre n'est
    pas jugée."""
    seuil = _verifier_le_seuil(taux_rejet_max)
    niveau = parametres.part_du_seuil * seuil
    par_origine: dict[Origine, list[tuple[datetime, int, LigneRegistre]]] = {}
    for rang, ligne in enumerate(registre):
        if ligne.pays is None or ligne.usine is None or ligne.ligne is None:
            continue
        if ligne.statut not in STATUTS_AVEC_UN_TAUX or ligne.nb_recues <= 0:
            continue
        _avec_fuseau(ligne.heure_arrivee, f"L'arrivée de la livraison {ligne.livraison_id}")
        par_origine.setdefault((ligne.pays, ligne.usine, ligne.ligne), []).append((ligne.heure_arrivee, rang, ligne))

    constats = []
    for origine, livraisons in sorted(par_origine.items()):
        fenetre = [ligne for _, _, ligne in sorted(livraisons, key=lambda entree: entree[:2])][-parametres.fenetre_livraisons:]
        if len(fenetre) < parametres.fenetre_livraisons:
            continue
        taux = sum(ligne.nb_rejetees for ligne in fenetre) / sum(ligne.nb_recues for ligne in fenetre)
        if taux > niveau:
            bloquees = sum(ligne.statut == StatutLivraison.BLOQUEE.value for ligne in fenetre)
            constats.append(ConstatTauxRejet(TAUX_DE_REJET_ELEVE, *origine, taux, niveau, seuil, len(fenetre), bloquees))
    return constats
