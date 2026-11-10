"""Réception d'une livraison : la structure d'abord, les lignes ensuite, UNE seule décision à la fin.

Ordre (décision A) : si la structure est invalide, la livraison est REFUSEE et aucune ligne n'est lue ; sinon le statut est celui du
contrat (ACCEPTEE, ACCEPTEE_AVEC_REJETS, BLOQUEE). Le résultat est un enregistrement : c'est lui qui deviendra une ligne du registre
des livraisons. Ce module n'écrit nulle part et ne lit aucun fichier : en-tête et lignes lui sont donnés déjà lus.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rd_manuf.contrat import Contrat, ResultatValidation, StatutLivraison, valider_livraison
from rd_manuf.structure import ProblemeStructure, controler_structure


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


def receptionner_livraison(contrat: Contrat, livraison_id: str, colonnes_recues: list[str], lignes: list[dict[str, Any]]) -> Reception:
    problemes = controler_structure(contrat, colonnes_recues, len(lignes))
    if problemes:
        return Reception(
            livraison_id=livraison_id,
            statut=StatutLivraison.REFUSEE,
            version_contrat=contrat.version,
            nb_recues=len(lignes),
            nb_valides=0,
            nb_rejetees=0,
            problemes_structure=tuple(problemes),
            resultat=None,
        )

    resultat = valider_livraison(contrat, lignes)
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
