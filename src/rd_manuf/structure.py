"""Contrôle de STRUCTURE d'une livraison : est-elle lisible avant même de regarder ses lignes ?

Décision : une structure invalide REFUSE la livraison entière, aucune ligne n'est lue. Lire « au mieux » (en ignorant une colonne
manquante) produirait des données fausses sans que personne ne le voie. Ce module ne fait que DIRE ce qui ne va pas ; il ne lit aucun
fichier et ne décide d'aucun statut : l'en-tête et le nombre de lignes lui sont donnés déjà lus.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum

from rd_manuf.contrat import Contrat


class CodeStructure(str, Enum):
    """Code STABLE d'un problème de structure : le registre et les alertes s'appuient dessus, jamais sur le texte du message."""

    FICHIER_VIDE = "FICHIER_VIDE"
    FICHIER_ILLISIBLE = "FICHIER_ILLISIBLE"    # impossible à lire (mauvais encodage, format inconnu, fichier abîmé)
    NOM_FICHIER_INVALIDE = "NOM_FICHIER_INVALIDE"    # le nom ne suit pas la convention : on ne sait pas d'où ni de quand vient le fichier
    LIVRAISON_EN_CONFLIT = "LIVRAISON_EN_CONFLIT"    # même origine et même instant qu'une livraison déjà intégrée, mais contenu différent
    COLONNE_MANQUANTE = "COLONNE_MANQUANTE"
    COLONNE_INCONNUE = "COLONNE_INCONNUE"
    COLONNE_DUPLIQUEE = "COLONNE_DUPLIQUEE"


@dataclass(frozen=True)
class ProblemeStructure:
    code: CodeStructure
    colonnes: tuple[str, ...]   # colonnes concernées, vide si le problème n'en désigne aucune
    message: str                # texte pour un humain


def _probleme(code: CodeStructure, colonnes: tuple[str, ...], message: str) -> ProblemeStructure:
    return ProblemeStructure(code, colonnes, message)


def controler_structure(contrat: Contrat, colonnes_recues: list[str], nb_lignes: int) -> list[ProblemeStructure]:
    """Liste TOUS les problèmes de structure (jamais le premier seulement), un par code, triés par code.

    Les noms sont comparés à l'identique : `ID_LOT` ou ` id_lot` ne sont pas `id_lot`. L'ordre des colonnes n'a pas d'importance.
    """
    if not colonnes_recues:
        return [_probleme(CodeStructure.FICHIER_VIDE, (), "Le fichier est vide : aucun en-tête.")]

    attendues = [colonne.nom for colonne in contrat.colonnes]
    problemes: list[ProblemeStructure] = []

    if nb_lignes == 0:
        problemes.append(_probleme(CodeStructure.FICHIER_VIDE, (), "Le fichier ne contient aucune ligne de données."))

    manquantes = tuple(nom for nom in attendues if nom not in colonnes_recues)
    if manquantes:
        problemes.append(_probleme(CodeStructure.COLONNE_MANQUANTE, manquantes, f"Colonnes manquantes : {', '.join(manquantes)}."))

    inconnues = tuple(dict.fromkeys(nom for nom in colonnes_recues if nom not in attendues))   # sans doublon, dans l'ordre d'arrivée
    if inconnues:
        problemes.append(_probleme(CodeStructure.COLONNE_INCONNUE, inconnues, f"Colonnes inconnues du contrat : {', '.join(inconnues)}."))

    dupliquees = tuple(nom for nom, nombre in Counter(colonnes_recues).items() if nombre > 1)
    if dupliquees:
        problemes.append(_probleme(CodeStructure.COLONNE_DUPLIQUEE, dupliquees, f"Colonnes en double : {', '.join(dupliquees)}."))

    return sorted(problemes, key=lambda probleme: probleme.code.value)
