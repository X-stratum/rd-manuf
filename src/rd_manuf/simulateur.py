"""Simulateur de livraisons : fabrique des livraisons conformes au contrat, avec des anomalies demandées PAR CODE D'ERREUR.

Il sert à nourrir le bronze, le silver et les alertes avec des défauts CONNUS D'AVANCE, sans attendre l'usine.
Les bornes et les domaines viennent du contrat ; seul le format de la clé (`LOT-000042`) est connu du simulateur.
Même graine = même livraison : c'est ce qui permet de rejouer un incident.
"""
from __future__ import annotations

import csv
import random
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rd_manuf.contrat import CodeErreur, Colonne, Contrat

# ==============================================================================
# ⚙️ PARAMÈTRES DE LA SIMULATION
# ==============================================================================
DATE_DEBUT = date(2026, 1, 1)
NB_JOURS_SIMULES = 365
PROBABILITE_TEMPERATURE_ABSENTE = 0.1   # capteur parfois indisponible : la colonne est facultative
DECIMALES_TEMPERATURE = 1
VALEUR_TEXTE_INVALIDE = "beaucoup"
VALEUR_HORS_DOMAINE = "INCONNUE"
CLE_MAL_FORMEE = "LOT-12"

Ligne = dict[str, Any]


def _colonne(contrat: Contrat, nom: str) -> Colonne:
    return next(colonne for colonne in contrat.colonnes if colonne.nom == nom)


# ==============================================================================
# ✅ UNE LIGNE SAINE
# ==============================================================================
def _ligne_saine(contrat: Contrat, rng: random.Random, numero: int) -> Ligne:
    produite = _colonne(contrat, "quantite_produite")
    rebut = _colonne(contrat, "quantite_rebut")
    temperature = _colonne(contrat, "temperature_c")

    quantite = rng.randint(int(produite.minimum), int(produite.maximum))
    temperature_absente = rng.random() < PROBABILITE_TEMPERATURE_ABSENTE
    valeur_temperature = rng.uniform(temperature.minimum, temperature.maximum)
    return {
        "id_lot": f"LOT-{numero:06d}",
        "id_ligne": rng.choice(_colonne(contrat, "id_ligne").valeurs_autorisees),
        "date_production": (DATE_DEBUT + timedelta(days=rng.randrange(NB_JOURS_SIMULES))).isoformat(),
        "quantite_produite": quantite,
        "quantite_rebut": rng.randint(int(rebut.minimum), quantite),   # la règle du contrat : rebut <= production
        "temperature_c": None if temperature_absente else round(valeur_temperature, DECIMALES_TEMPERATURE),
    }


# ==============================================================================
# 💥 UNE ANOMALIE PAR CODE : chaque fabrique ne viole QUE son code
# ==============================================================================
def _hors_plage(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "temperature_c": _colonne(contrat, "temperature_c").maximum + 1}


def _obligatoire_manquant(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "date_production": None}


def _type_invalide(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "quantite_produite": VALEUR_TEXTE_INVALIDE}


def _motif_non_respecte(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "id_lot": CLE_MAL_FORMEE}


def _valeur_hors_domaine(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "id_ligne": VALEUR_HORS_DOMAINE}


def _regle_entre_colonnes(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    return {**ligne, "quantite_rebut": ligne["quantite_produite"] + 1}


def _cle_en_conflit(contrat: Contrat, ligne: Ligne, rng: random.Random) -> Ligne:
    """`ligne` est la COPIE d'une ligne saine : même clé, autre ligne de fabrication (donc contenu différent mais valide)."""
    autres = [valeur for valeur in _colonne(contrat, "id_ligne").valeurs_autorisees if valeur != ligne["id_ligne"]]
    return {**ligne, "id_ligne": rng.choice(autres)}


FABRIQUES_D_ANOMALIES: dict[CodeErreur, Callable[[Contrat, Ligne, random.Random], Ligne]] = {
    CodeErreur.OBLIGATOIRE_MANQUANT: _obligatoire_manquant,
    CodeErreur.TYPE_INVALIDE: _type_invalide,
    CodeErreur.HORS_PLAGE: _hors_plage,
    CodeErreur.MOTIF_NON_RESPECTE: _motif_non_respecte,
    CodeErreur.VALEUR_HORS_DOMAINE: _valeur_hors_domaine,
    CodeErreur.REGLE_ENTRE_COLONNES: _regle_entre_colonnes,
    CodeErreur.CLE_EN_CONFLIT: _cle_en_conflit,
}


# ==============================================================================
# 📦 UNE LIVRAISON
# ==============================================================================
def generer_livraison(
    contrat: Contrat,
    nb_lignes: int,
    graine: int,
    anomalies: dict[CodeErreur, int] | None = None,
) -> list[Ligne]:
    """`nb_lignes` lignes dont les dernières portent les anomalies demandées ({code: nombre}).

    Les anomalies REMPLACENT des lignes saines : le total reste `nb_lignes`.
    Un conflit de clé copie la clé d'une ligne saine, qui garde sa place : la seconde arrivée est celle qui sera rejetée.
    """
    anomalies = anomalies or {}
    nb_anomalies = sum(anomalies.values())
    nb_saines = nb_lignes - nb_anomalies
    if nb_saines < 0:
        raise ValueError(f"{nb_anomalies} anomalies demandées pour {nb_lignes} lignes.")
    if anomalies.get(CodeErreur.CLE_EN_CONFLIT, 0) > nb_saines:
        raise ValueError("Pas assez de lignes saines à contredire pour fabriquer ces conflits de clé.")

    rng = random.Random(graine)
    lignes = [_ligne_saine(contrat, rng, numero) for numero in range(1, nb_lignes + 1)]

    position = nb_saines
    for code in sorted(anomalies, key=lambda code: code.value):    # ordre fixe : la livraison reste reproductible
        for rang in range(anomalies[code]):
            modele = lignes[rang] if code == CodeErreur.CLE_EN_CONFLIT else lignes[position]
            lignes[position] = FABRIQUES_D_ANOMALIES[code](contrat, dict(modele), rng)
            position += 1
    return lignes


# ==============================================================================
# 📄 FICHIER CSV : ce que le bronze lira (tout y est du texte, une absence est un champ vide)
# ==============================================================================
def ecrire_livraison_csv(contrat: Contrat, lignes: list[Ligne], chemin: str | Path) -> None:
    with Path(chemin).open("w", encoding="utf-8", newline="") as fichier:
        ecrivain = csv.DictWriter(fichier, fieldnames=[colonne.nom for colonne in contrat.colonnes])
        ecrivain.writeheader()
        ecrivain.writerows(lignes)
