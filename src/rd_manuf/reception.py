"""Réception d'une livraison : la structure d'abord, les lignes ensuite, UNE seule décision à la fin.

Ordre (décision A) : si la structure est invalide, la livraison est REFUSEE et aucune ligne n'est lue ; sinon le statut est celui du
contrat (ACCEPTEE, ACCEPTEE_AVEC_REJETS, BLOQUEE). Le résultat est un enregistrement : c'est lui qui deviendra une ligne du registre
des livraisons. Ce module n'écrit nulle part et ne lit aucun fichier : en-tête et lignes lui sont donnés déjà lus.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rd_manuf.contrat import Contrat, LigneMalformee, ResultatValidation, StatutLivraison, valider_livraison
from rd_manuf.lecture import FichierIllisible, lire_selon_format
from rd_manuf.nom_fichier import NomFichier, NomFichierInvalide, lire_nom_fichier
from rd_manuf.structure import CodeStructure, ProblemeStructure, controler_structure


@dataclass(frozen=True)
class Reception:
    livraison_id: str
    statut: StatutLivraison
    version_contrat: str
    nb_recues: int                                        # lignes ARRIVÉES, même si la livraison est refusée
    nb_valides: int                                       # 0 si REFUSEE ou BLOQUEE : rien n'est intégré
    nb_rejetees: int                                      # 0 si REFUSEE : aucune ligne n'a été jugée
    problemes_structure: tuple[ProblemeStructure, ...]    # vide sauf si REFUSEE
    resultat: ResultatValidation | None                   # détail pour les alertes ; None si REFUSEE
    nom_fichier: NomFichier | None = None                 # d'où et de quand ; None si le nom est invalide ou sans fichier


def receptionner_livraison(
    contrat: Contrat,
    livraison_id: str,
    colonnes_recues: list[str],
    lignes: list[dict[str, Any]],
    lignes_malformees: Sequence[LigneMalformee] = (),
) -> Reception:
    nb_lignes_recues = len(lignes) + len(lignes_malformees)     # une ligne malformée est une ligne reçue : le fichier n'est pas vide
    problemes = controler_structure(contrat, colonnes_recues, nb_lignes_recues)
    if problemes:
        return Reception(
            livraison_id=livraison_id,
            statut=StatutLivraison.REFUSEE,
            version_contrat=contrat.version,
            nb_recues=nb_lignes_recues,
            nb_valides=0,
            nb_rejetees=0,
            problemes_structure=tuple(problemes),
            resultat=None,
        )

    resultat = valider_livraison(contrat, lignes, lignes_malformees)
    return Reception(
        livraison_id=livraison_id,
        statut=resultat.statut,
        version_contrat=resultat.version_contrat,
        nb_recues=resultat.nb_recues,
        nb_valides=len(resultat.lignes_valides),
        nb_rejetees=len(resultat.lignes_rejetees),
        problemes_structure=(),
        resultat=resultat,
    )


def refuser_fichier_illisible(contrat: Contrat, livraison_id: str, erreur: FichierIllisible) -> Reception:
    """Refus d'une livraison dont le fichier n'a pas pu être lu : ni colonne ni ligne, un seul problème de structure.

    Le motif technique de l'erreur est gardé dans le message, pour l'équipe data ; l'émetteur, lui, reçoit l'alerte du catalogue.
    """
    return _refus(contrat, livraison_id, ProblemeStructure(CodeStructure.FICHIER_ILLISIBLE, (), str(erreur)))


def refuser_nom_invalide(contrat: Contrat, livraison_id: str, erreur: NomFichierInvalide) -> Reception:
    """Refus d'une livraison dont le nom ne suit pas la convention : rien n'a été lu, un seul problème de structure."""
    return _refus(contrat, livraison_id, ProblemeStructure(CodeStructure.NOM_FICHIER_INVALIDE, (), str(erreur)))


def refuser_conflit(contrat: Contrat, livraison_id: str, nom: NomFichier, livraison_deja_integree: str) -> Reception:
    """Refus d'un fichier de même origine et de même instant qu'une livraison déjà intégrée, mais de contenu différent.

    Rien n'est lu ni jugé : on ne sait pas laquelle des deux versions est la bonne, et ignorer celle-ci en silence perdrait des données.
    """
    message = (
        f"Une livraison de même origine et de même instant est déjà intégrée ({livraison_deja_integree}) avec un contenu différent. "
        "Renvoyer le fichier sous un nouvel instant si c'est une correction."
    )
    reception = _refus(contrat, livraison_id, ProblemeStructure(CodeStructure.LIVRAISON_EN_CONFLIT, (), message))
    return replace(reception, nom_fichier=nom)


def _refus(contrat: Contrat, livraison_id: str, probleme: ProblemeStructure) -> Reception:
    return Reception(
        livraison_id=livraison_id,
        statut=StatutLivraison.REFUSEE,
        version_contrat=contrat.version,
        nb_recues=0,
        nb_valides=0,
        nb_rejetees=0,
        problemes_structure=(probleme,),
        resultat=None,
    )


def receptionner_fichier(contrat: Contrat, chemin: str | Path) -> Reception:
    """Le point d'entrée complet sur un fichier : le nom d'abord, puis la lecture selon le format, puis la réception.

    Un nom invalide est refusé AVANT toute lecture ; un fichier illisible est refusé proprement. L'identifiant de la livraison est
    le nom du fichier. Un fichier introuvable lève encore une erreur brute (`FileNotFoundError`) : ce n'est pas un défaut du fichier reçu.
    """
    chemin = Path(chemin)
    try:
        nom = lire_nom_fichier(chemin.name)
    except NomFichierInvalide as erreur:
        return refuser_nom_invalide(contrat, chemin.name, erreur)
    try:
        fichier = lire_selon_format(chemin, nom.format)
    except FichierIllisible as erreur:
        return replace(refuser_fichier_illisible(contrat, chemin.name, erreur), nom_fichier=nom)
    reception = receptionner_livraison(contrat, chemin.name, fichier.colonnes, fichier.lignes, fichier.lignes_malformees)
    return replace(reception, nom_fichier=nom)
