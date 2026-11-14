"""L'enchaînement d'une livraison : du fichier reçu au registre, au bronze, au silver et aux alertes, en un seul appel.

Chaque étape existe déjà et se teste seule ; ce module les relie dans l'ordre, une seule fois, pour que le scénario, les tests de volume et
l'écrivain Spark n'aient pas chacun à réinventer cet ordre. Il ne décide RIEN lui-même : c'est le registre qui dit si la livraison entre
(`STATUTS_BRONZE`), et le silver qui garantit de décider comme la réception.

L'ÉTAT reste chez l'appelant : le registre déjà écrit et les lignes silver déjà intégrées sont passés en paramètres, jamais lus ni écrits
ici, et l'heure d'arrivée est passée elle aussi (jamais lue sur l'horloge). Le résultat contient tout ce qu'il y a à ajouter ; l'appelant
l'ajoute à son état, aujourd'hui des listes, demain des tables.

Lectures du fichier, trois au plus : son empreinte (pour reconnaître un rejeu ou un conflit), la réception, puis le bronze. Un nom invalide
n'en fait aucune, un rejeu ou un conflit de livraison une seule, un refus de structure deux. À réduire plus tard : la réception pourrait
garder sa lecture pour le bronze.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rd_manuf.alertes import Alerte, Catalogue, ajouter_conflits_entre_livraisons, router_alertes_reception
from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee, construire_lignes_bronze, construire_lignes_coupees
from rd_manuf.contrat import Contrat
from rd_manuf.lecture import lire_selon_format
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.reception import Reception
from rd_manuf.registre import LigneRegistre, traiter_fichier
from rd_manuf.silver import LigneSilver, ResultatSilver, construire_silver


def _silver_vide() -> ResultatSilver:
    """Un résultat vide NEUF à chaque appel : ses listes sont modifiables, un objet partagé serait pollué d'une livraison à l'autre."""
    return ResultatSilver(lignes=[], quarantaine=[])


@dataclass(frozen=True)
class ResultatLivraison:
    """Tout ce qu'une livraison ajoute à l'état de l'appelant."""

    ligne_registre: LigneRegistre              # toujours : une ligne par livraison reçue, rejeu compris
    reception: Reception | None                # None pour un rejeu : rien n'est relu ni rejugé
    lignes_bronze: list[LigneBronze]           # vide si la livraison n'entre pas (refusée, rejeu)
    lignes_coupees: list[LigneBronzeCoupee]
    silver: ResultatSilver                     # vide pour une livraison refusée, rejouée ou bloquée
    alertes: list[Alerte]                      # vide pour un rejeu ; celles de la réception et les conflits avec une plus ancienne


def traiter_livraison(
    contrat: Contrat,
    catalogue: Catalogue,
    chemin: str | Path,
    registre: Sequence[LigneRegistre],
    deja_integrees: Sequence[LigneSilver],
    heure_arrivee: datetime,
) -> ResultatLivraison:
    """Fait entrer une livraison dans toutes les couches, selon ce que le registre décide.

    `registre` : les lignes déjà écrites (pour reconnaître un rejeu ou un conflit). `deja_integrees` : les lignes silver déjà intégrées
    (pour les lots déjà vus). Aucun des deux n'est modifié. Un fichier introuvable lève encore `FileNotFoundError`, une heure d'arrivée
    sans fuseau une `ValueError` : ce sont des erreurs de l'appelant, pas des défauts de la livraison.
    """
    chemin = Path(chemin)
    reception, ligne_registre = traiter_fichier(contrat, chemin, registre, heure_arrivee)
    if reception is None:
        return ResultatLivraison(ligne_registre, None, [], [], _silver_vide(), [])

    alertes = router_alertes_reception(catalogue, reception)
    if ligne_registre.statut not in STATUTS_BRONZE:
        return ResultatLivraison(ligne_registre, reception, [], [], _silver_vide(), alertes)

    fichier = lire_selon_format(chemin, lire_nom_fichier(chemin.name).format)
    lignes_bronze = construire_lignes_bronze(contrat, ligne_registre, fichier)
    lignes_coupees = construire_lignes_coupees(ligne_registre, fichier)
    silver = construire_silver(contrat, ligne_registre, lignes_bronze, lignes_coupees, deja_integrees=deja_integrees)
    alertes = ajouter_conflits_entre_livraisons(
        catalogue, alertes, ligne_registre.livraison_id, silver.nb_conflits_entre_livraisons, contrat.cle_primaire
    )
    return ResultatLivraison(ligne_registre, reception, lignes_bronze, lignes_coupees, silver, alertes)
