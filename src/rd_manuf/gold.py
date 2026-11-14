"""Gold : la production par jour, pays, usine et ligne de fabrication, calculée depuis le silver.

C'est une fonction PURE : des lignes silver en entrée, des lignes gold en sortie, rien d'écrit, aucun moteur SQL. Elle sert de RÉFÉRENCE :
le SQL d'agrégation qui tournera sur Databricks se comparera à elle sur la même journée.

Un taux se calcule toujours sur les SOMMES (rebut total / production totale), jamais en moyennant les taux de lots : un lot de 10 pièces
n'a pas le même poids qu'un lot de 100 000. Une production nulle donne un taux vide, pas une division par zéro. La température n'est
calculée que sur les mesures présentes (une température de 0 est une mesure) et n'est pas arrondie : l'arrondi est affaire d'affichage.

Le grain est la DATE DE PRODUCTION, pas la date d'arrivée : une livraison tardive ou corrigée modifie un ancien jour. Le gold se recalcule
donc en entier depuis le silver, ce qui le rend sûr à rejouer.

Le gold lit ces colonnes du silver par leur nom (`COLONNES_LUES`) : un test les relie au contrat, pour qu'un renommage fasse échouer la CI.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from rd_manuf.silver import LigneSilver

COLONNES_OBLIGATOIRES = ("id_ligne", "date_production", "quantite_produite", "quantite_rebut")
COLONNE_TEMPERATURE = "temperature_c"
COLONNES_LUES = (*COLONNES_OBLIGATOIRES, COLONNE_TEMPERATURE)


@dataclass(frozen=True)
class LigneProductionQuotidienne:
    pays: str
    usine: str
    id_ligne: str                                  # la ligne de fabrication (L1, L2, L3)
    date_production: date
    nb_lots: int
    quantite_produite: int                         # la somme sur les lots
    quantite_rebut: int
    taux_rebut: float | None                       # rebut total / production totale ; vide si la production est nulle
    nb_mesures_temperature: int                    # les lots qui ont une mesure : la température est calculée sur eux seuls
    temperature_moyenne: float | None              # vides, toutes les trois, sans aucune mesure
    temperature_min: float | None
    temperature_max: float | None


def _valeurs(ligne: LigneSilver) -> dict[str, Any]:
    """Les valeurs lues par le gold, ou une erreur qui nomme la colonne et le lot : jamais un calcul fait sur un trou."""
    for nom in COLONNES_LUES:
        if nom not in ligne.valeurs:
            raise ValueError(f"Le silver n'a pas la colonne « {nom} » (livraison {ligne.livraison_id}, rang {ligne.numero_ligne}).")
    for nom in COLONNES_OBLIGATOIRES:
        if ligne.valeurs[nom] is None:
            raise ValueError(f"La colonne « {nom} » est vide au silver (livraison {ligne.livraison_id}, rang {ligne.numero_ligne}).")
    return ligne.valeurs


def calculer_production_quotidienne(lignes_silver: Iterable[LigneSilver]) -> list[LigneProductionQuotidienne]:
    """Une ligne par (pays, usine, ligne de fabrication, jour de production), triée dans cet ordre. Ne modifie pas ses entrées."""
    groupes: dict[tuple[str, str, str, date], list[dict[str, Any]]] = {}
    for ligne in lignes_silver:
        valeurs = _valeurs(ligne)
        groupes.setdefault((ligne.pays, ligne.usine, valeurs["id_ligne"], valeurs["date_production"]), []).append(valeurs)

    resultat = []
    for (pays, usine, id_ligne, jour), lots in sorted(groupes.items()):
        produite = sum(lot["quantite_produite"] for lot in lots)
        rebut = sum(lot["quantite_rebut"] for lot in lots)
        mesures = [lot[COLONNE_TEMPERATURE] for lot in lots if lot[COLONNE_TEMPERATURE] is not None]
        resultat.append(LigneProductionQuotidienne(
            pays=pays,
            usine=usine,
            id_ligne=id_ligne,
            date_production=jour,
            nb_lots=len(lots),
            quantite_produite=produite,
            quantite_rebut=rebut,
            taux_rebut=rebut / produite if produite else None,
            nb_mesures_temperature=len(mesures),
            temperature_moyenne=math.fsum(mesures) / len(mesures) if mesures else None,
            temperature_min=min(mesures) if mesures else None,
            temperature_max=max(mesures) if mesures else None,
        ))
    return resultat
