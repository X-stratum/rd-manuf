"""Simulateur de livraisons : fabrique des livraisons conformes au contrat, avec des anomalies demandées PAR CODE D'ERREUR.

Il sert à nourrir le bronze, le silver et les alertes avec des défauts CONNUS D'AVANCE, sans attendre l'usine.
Les bornes et les domaines viennent du contrat ; seul le format de la clé (`LOT-000042`) est connu du simulateur.
Même graine = même livraison : c'est ce qui permet de rejouer un incident.

Il fabrique aussi ce qui entoure les lignes : le NOM d'un fichier (avec la convention de `nom_fichier.py`, qui le valide), la
numérotation des lots (`premier_lot`, pour que deux livraisons d'une même usine aient des numéros distincts) et le fichier CSV ou Parquet.
Un Parquet a des colonnes TYPÉES d'après le contrat : un texte dans une colonne numérique ne peut pas s'y écrire, ce défaut
(`TYPE_INVALIDE`) n'existe qu'en CSV.
"""
from __future__ import annotations

import csv
import random
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from rd_manuf.contrat import CodeErreur, Colonne, Contrat, convertir_valeur
from rd_manuf.nom_fichier import FORMAT_DATE, PREFIXE, lire_nom_fichier

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
DERNIER_NUMERO_DE_LOT = 999_999          # `LOT-` suivi de 6 chiffres, le motif du contrat : un 7e chiffre serait refusé

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
    premier_lot: int = 1,
) -> list[Ligne]:
    """`nb_lignes` lignes dont les dernières portent les anomalies demandées ({code: nombre}).

    Les anomalies REMPLACENT des lignes saines : le total reste `nb_lignes`.
    Un conflit de clé copie la clé d'une ligne saine, qui garde sa place : la seconde arrivée est celle qui sera rejetée.
    Les lots sont numérotés à partir de `premier_lot` (1 par défaut) : seuls les numéros changent, les autres valeurs ne dépendent que de la
    graine. Deux livraisons d'une même usine reçoivent ainsi des plages de numéros distinctes, qui ne se gênent pas au silver.
    """
    if premier_lot < 1:
        raise ValueError(f"premier_lot doit valoir au moins 1 : {premier_lot}.")
    if nb_lignes > 0 and premier_lot + nb_lignes - 1 > DERNIER_NUMERO_DE_LOT:
        raise ValueError(
            f"premier_lot={premier_lot} avec {nb_lignes} lignes : le dernier lot serait LOT-{premier_lot + nb_lignes - 1}, au-delà de "
            f"LOT-{DERNIER_NUMERO_DE_LOT} (6 chiffres, comme le motif du contrat)."
        )
    anomalies = anomalies or {}
    for code, nombre in anomalies.items():
        if not isinstance(code, CodeErreur):
            raise ValueError(f"Code d'erreur inconnu : {code!r}.")
        if code not in FABRIQUES_D_ANOMALIES:
            raise ValueError(f"{code.value} ne se fabrique pas par une valeur de ligne : elle vient de la forme du fichier.")
        if nombre < 0:
            raise ValueError(f"Nombre d'anomalies négatif pour {code.value} : {nombre}.")
    nb_anomalies = sum(anomalies.values())
    nb_saines = nb_lignes - nb_anomalies
    if nb_saines < 0:
        raise ValueError(f"{nb_anomalies} anomalies demandées pour {nb_lignes} lignes.")
    if anomalies.get(CodeErreur.CLE_EN_CONFLIT, 0) > nb_saines:
        raise ValueError("Pas assez de lignes saines à contredire pour fabriquer ces conflits de clé.")

    rng = random.Random(graine)
    lignes = [_ligne_saine(contrat, rng, numero) for numero in range(premier_lot, premier_lot + nb_lignes)]

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


# ==============================================================================
# 🧱 FICHIER PARQUET : des colonnes TYPÉES d'après le contrat (ce que l'usine enverrait, au lieu du texte d'un CSV)
# ==============================================================================
def types_arrow_du_contrat() -> dict[str, Any]:
    """Le type Parquet de chaque type du contrat. `decimal` est un double : le contrat ne dit ni précision ni échelle."""
    import pyarrow as pa  # importé ici : le simulateur n'a besoin de ce paquet lourd que pour écrire un Parquet

    return {"chaine": pa.string(), "entier": pa.int64(), "decimal": pa.float64(), "date": pa.date32()}


def _valeur_ecrivable(colonne: Colonne, valeur: Any) -> Any:
    """Une date écrite en texte ISO (comme le simulateur la fabrique) devient une vraie date ; le reste est laissé à Parquet."""
    if colonne.type == "date" and isinstance(valeur, str):
        try:
            return date.fromisoformat(valeur)
        except ValueError as erreur:
            raise ValueError(f"{colonne.nom} : « {valeur} » n'est pas une date, elle ne peut pas être écrite en Parquet.") from erreur
    return valeur


def ecrire_livraison_parquet(contrat: Contrat, lignes: list[Ligne], chemin: str | Path) -> None:
    """Écrit les lignes en Parquet, chaque colonne ayant le type du contrat ; une absence (`None`) devient un vrai `null`.

    Une valeur qui n'a pas le type de sa colonne ne peut pas s'y écrire (un texte dans une colonne entière) : c'est refusé avec la colonne
    et la valeur, sans écrire de fichier. Ce défaut n'existe qu'en CSV. Il en va de même de ce que le contrat refuserait comme type
    invalide (un décimal non entier dans une colonne entière, un booléen, une date avec heure) : Parquet le convertirait sans rien dire.
    Les lignes reçues ne sont pas modifiées.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    types = types_arrow_du_contrat()
    colonnes = []
    for colonne in contrat.colonnes:
        valeurs = [_valeur_ecrivable(colonne, ligne.get(colonne.nom)) for ligne in lignes]
        fautive = next((valeur for valeur in valeurs if valeur is not None and not _respecte_le_contrat(colonne, valeur)), None)
        cause: Exception | None = None
        if fautive is None:
            try:
                colonnes.append(pa.array(valeurs, type=types[colonne.type]))
                continue
            except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as erreur:
                cause = erreur
                fautive = next((v for v in valeurs if v is not None and not _est_ecrivable(v, types[colonne.type])), "?")
        raise ValueError(
            f"{colonne.nom} : la valeur « {fautive} » n'a pas le type {colonne.type} de sa colonne, elle ne peut pas être écrite en "
            f"Parquet (un Parquet est typé : ce défaut n'existe qu'en CSV)."
        ) from cause
    schema = pa.schema([(colonne.nom, types[colonne.type]) for colonne in contrat.colonnes])
    pq.write_table(pa.Table.from_arrays(colonnes, schema=schema), chemin)


def _respecte_le_contrat(colonne: Colonne, valeur: Any) -> bool:
    """Le contrat accepte-t-il cette valeur comme étant du type de la colonne ? Parquet, lui, convertit sans rien dire (1.5 en 1)."""
    try:
        convertir_valeur(colonne.type, valeur)
    except (ValueError, TypeError):
        return False
    return True


def _est_ecrivable(valeur: Any, type_arrow: Any) -> bool:
    """Parquet accepte-t-il cette valeur dans une colonne de ce type ? Sert seulement à retrouver la valeur fautive d'une erreur."""
    import pyarrow as pa

    try:
        pa.scalar(valeur, type=type_arrow)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        return False
    return True


# ==============================================================================
# 🏷️ NOM DE FICHIER : la convention est celle de `nom_fichier.py`, qui valide ce qu'on fabrique
# ==============================================================================
def nom_de_fichier(pays: str, usine: str, ligne: str, instant: datetime, format: str) -> str:
    """Le nom d'une livraison : `livraison_production_{PAYS}_{USINE}_{LIGNE}_{instant UTC}Z.{format}`.

    L'instant doit avoir un fuseau (il est écrit en UTC) et tomber sur une seconde entière : le nom ne porte pas de fraction, le relire
    donnerait un autre instant. Une pièce qui ne suit pas la convention lève `NomFichierInvalide`, en relisant le nom avec
    `lire_nom_fichier`.
    """
    if instant.utcoffset() is None:
        raise ValueError("L'instant du nom de fichier doit avoir un fuseau (il est écrit en UTC) : sans fuseau, il ne se compare pas.")
    if instant.microsecond:
        raise ValueError(f"L'instant du nom de fichier doit tomber sur une seconde entière : {instant.isoformat()}.")
    nom = f"{PREFIXE}_{pays}_{usine}_{ligne}_{instant.astimezone(UTC).strftime(FORMAT_DATE)}Z.{format}"
    lire_nom_fichier(nom)
    return nom
