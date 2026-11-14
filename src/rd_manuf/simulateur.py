"""Simulateur de livraisons : fabrique des livraisons conformes au contrat, avec des anomalies demandées PAR CODE D'ERREUR.

Il sert à nourrir le bronze, le silver et les alertes avec des défauts CONNUS D'AVANCE, sans attendre l'usine.
Les bornes et les domaines viennent du contrat ; seul le format de la clé (`LOT-000042`) est connu du simulateur.
Même graine = même livraison : c'est ce qui permet de rejouer un incident.

Il fabrique aussi ce qui entoure les lignes : le NOM d'un fichier (avec la convention de `nom_fichier.py`, qui le valide), la
numérotation des lots (`premier_lot`, pour que deux livraisons d'une même usine aient des numéros distincts) et le fichier CSV ou Parquet.
Un Parquet a des colonnes TYPÉES d'après le contrat : un texte dans une colonne numérique ne peut pas s'y écrire, ce défaut
(`TYPE_INVALIDE`) n'existe qu'en CSV.
Les défauts de FORME (`DefautDeFichier` : colonne en trop, fichier vide ou illisible, ligne coupée) se demandent à l'écriture du fichier,
comme les anomalies de valeur se demandent à `generer_livraison` : `{défaut: nombre}`. Un défaut qui n'existe pas dans un format (une ligne
coupée en Parquet) est refusé avec un message clair, sans écrire de fichier.
"""
from __future__ import annotations

import csv
import random
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from enum import Enum
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
PREFIXE_COLONNE_INCONNUE = "colonne_inconnue"   # une colonne en trop s'appelle colonne_inconnue_1, colonne_inconnue_2...
VALEUR_COLONNE_INCONNUE = "?"
OCTETS_NON_UTF8 = b"\xe9\n"                       # un « é » encodé en latin-1 : invalide en UTF-8, le fichier ne se décode plus

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
# 🧩 DÉFAUTS DE FORME : ce qui abîme le FICHIER plutôt que les valeurs d'une ligne
# ==============================================================================
class DefautDeFichier(str, Enum):
    """Un défaut de forme, demandé à l'écriture. Le nombre associé dit combien de colonnes ou de lignes sont touchées.

    Les colonnes manquantes sont les DERNIÈRES du contrat, les doublons les PREMIÈRES colonnes du fichier, les lignes coupées les DERNIÈRES
    lignes : le fichier ne dépend que de la demande, pas d'un tirage. Sans nombre à donner, `FICHIER_VIDE` et `FICHIER_ILLISIBLE` valent 1.
    """

    COLONNE_MANQUANTE = "COLONNE_MANQUANTE"      # colonnes du contrat retirées de l'en-tête et de chaque ligne
    COLONNE_INCONNUE = "COLONNE_INCONNUE"        # colonnes qui n'existent pas dans le contrat, ajoutées après les autres
    COLONNE_DUPLIQUEE = "COLONNE_DUPLIQUEE"      # colonnes écrites deux fois dans l'en-tête
    FICHIER_VIDE = "FICHIER_VIDE"                # CSV seulement : aucun octet (un Parquet sans octet est illisible, pas vide)
    FICHIER_ILLISIBLE = "FICHIER_ILLISIBLE"      # CSV : octets qui ne sont pas de l'UTF-8 ; Parquet : fichier tronqué
    LIGNE_COUPEE = "LIGNE_COUPEE"                # CSV seulement : lignes à moitié écrites (un Parquet n'a pas de ligne « coupée »)


FICHIER_ENTIER = (DefautDeFichier.FICHIER_VIDE, DefautDeFichier.FICHIER_ILLISIBLE)     # défauts du fichier entier : seuls, et une fois
RAISONS_EN_PARQUET = {      # les défauts qui n'existent qu'en CSV, et pourquoi
    DefautDeFichier.FICHIER_VIDE: "un Parquet sans octet est illisible (pour zéro ligne, écrire une livraison vide)",
    DefautDeFichier.LIGNE_COUPEE: "un Parquet n'a pas de ligne coupée : ses colonnes sont typées",
}


def _verifier_defauts(
    contrat: Contrat, nb_lignes: int, defauts: dict[DefautDeFichier, int] | None, format: str
) -> dict[DefautDeFichier, int]:
    """Refuse une demande impossible AVANT d'écrire quoi que ce soit ; rend les défauts réellement demandés (0 n'en est pas un)."""
    defauts = defauts or {}
    for defaut, nombre in defauts.items():
        if not isinstance(defaut, DefautDeFichier):
            raise ValueError(f"Défaut de fichier inconnu : {defaut!r}.")
        if nombre < 0:
            raise ValueError(f"Nombre négatif pour {defaut.value} : {nombre}.")
    demandes = {defaut: nombre for defaut, nombre in defauts.items() if nombre > 0}

    for defaut in demandes:
        if format == "parquet" and defaut in RAISONS_EN_PARQUET:
            raison = RAISONS_EN_PARQUET[defaut]
            raise ValueError(f"{defaut.value} n'existe pas en Parquet : {raison}.")
    for defaut in FICHIER_ENTIER:
        if defaut in demandes and (demandes[defaut] != 1 or len(demandes) > 1):
            raise ValueError(f"{defaut.value} se demande seul et une seule fois : il touche le fichier entier, rien d'autre n'a de sens.")

    nb_colonnes = len(contrat.colonnes)
    manquantes = demandes.get(DefautDeFichier.COLONNE_MANQUANTE, 0)
    if manquantes >= nb_colonnes:
        raise ValueError(f"COLONNE_MANQUANTE : {manquantes} colonnes retirées sur {nb_colonnes}, il doit en rester au moins une.")
    gardees = nb_colonnes - manquantes
    doublons = demandes.get(DefautDeFichier.COLONNE_DUPLIQUEE, 0)
    if doublons > gardees:
        raise ValueError(f"COLONNE_DUPLIQUEE : {doublons} colonnes à dupliquer, mais il n'en reste que {gardees} dans le fichier.")
    coupees = demandes.get(DefautDeFichier.LIGNE_COUPEE, 0)
    if coupees > nb_lignes:
        raise ValueError(f"LIGNE_COUPEE : {coupees} lignes à couper, mais le fichier n'en a que {nb_lignes}.")
    if coupees and gardees + doublons + demandes.get(DefautDeFichier.COLONNE_INCONNUE, 0) < 2:
        raise ValueError("LIGNE_COUPEE : une ligne d'une seule colonne n'est jamais malformée, elle serait vide ou entière.")
    return demandes


def _colonnes_du_fichier(contrat: Contrat, defauts: dict[DefautDeFichier, int]) -> list[tuple[str, Colonne | None]]:
    """L'en-tête à écrire : (nom, colonne du contrat d'où vient la valeur ; `None` pour une colonne inconnue du contrat)."""
    gardees = list(contrat.colonnes)
    if nombre := defauts.get(DefautDeFichier.COLONNE_MANQUANTE, 0):
        gardees = gardees[:-nombre]
    entete: list[tuple[str, Colonne | None]] = [(colonne.nom, colonne) for colonne in gardees]
    entete += [(f"{PREFIXE_COLONNE_INCONNUE}_{rang}", None) for rang in range(1, defauts.get(DefautDeFichier.COLONNE_INCONNUE, 0) + 1)]
    entete += [(colonne.nom, colonne) for colonne in gardees[: defauts.get(DefautDeFichier.COLONNE_DUPLIQUEE, 0)]]
    return entete


# ==============================================================================
# 📄 FICHIER CSV : ce que le bronze lira (tout y est du texte, une absence est un champ vide)
# ==============================================================================
def ecrire_livraison_csv(
    contrat: Contrat, lignes: list[Ligne], chemin: str | Path, defauts: dict[DefautDeFichier, int] | None = None
) -> None:
    """Écrit les lignes en CSV (UTF-8, en-tête = colonnes du contrat). `defauts` : défauts de forme à fabriquer, `{défaut: nombre}`.

    Une ligne coupée garde la première moitié de ses champs : elle remplace l'une des DERNIÈRES lignes (celles que `generer_livraison`
    réserve à ses anomalies, qui disparaissent alors : seule la coupure est jugée). Les lignes reçues ne sont pas modifiées.
    """
    demandes = _verifier_defauts(contrat, len(lignes), defauts, "csv")
    chemin = Path(chemin)
    if DefautDeFichier.FICHIER_VIDE in demandes:
        chemin.write_bytes(b"")
        return

    entete = _colonnes_du_fichier(contrat, demandes)
    noms = {colonne.nom for colonne in contrat.colonnes}
    lignes_ecrites = []
    for ligne in lignes:
        if inconnues := set(ligne) - noms:
            raise ValueError(f"Une ligne porte des champs qui ne sont pas des colonnes du contrat : {sorted(inconnues)}.")
        lignes_ecrites.append([VALEUR_COLONNE_INCONNUE if colonne is None else ligne.get(colonne.nom) for _, colonne in entete])
    if nombre := demandes.get(DefautDeFichier.LIGNE_COUPEE, 0):
        for rang in range(len(lignes_ecrites) - nombre, len(lignes_ecrites)):
            lignes_ecrites[rang] = lignes_ecrites[rang][: len(entete) // 2]

    with chemin.open("w", encoding="utf-8", newline="") as fichier:
        ecrivain = csv.writer(fichier)
        ecrivain.writerow([nom for nom, _ in entete])
        ecrivain.writerows(lignes_ecrites)
    if DefautDeFichier.FICHIER_ILLISIBLE in demandes:
        with chemin.open("ab") as fichier:
            fichier.write(OCTETS_NON_UTF8)


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


def ecrire_livraison_parquet(
    contrat: Contrat, lignes: list[Ligne], chemin: str | Path, defauts: dict[DefautDeFichier, int] | None = None
) -> None:
    """Écrit les lignes en Parquet, chaque colonne ayant le type du contrat ; une absence (`None`) devient un vrai `null`.

    Une valeur qui n'a pas le type de sa colonne ne peut pas s'y écrire (un texte dans une colonne entière) : c'est refusé avec la colonne
    et la valeur, sans écrire de fichier. Ce défaut n'existe qu'en CSV. Il en va de même de ce que le contrat refuserait comme type
    invalide (un décimal non entier dans une colonne entière, un booléen, une date avec heure) : Parquet le convertirait sans rien dire.
    Les lignes reçues ne sont pas modifiées. `defauts` : défauts de forme à fabriquer, `{défaut: nombre}` ; une ligne coupée ou un fichier
    sans octet n'existent pas en Parquet (refusé). Un fichier illisible est un Parquet tronqué à la moitié de ses octets.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    demandes = _verifier_defauts(contrat, len(lignes), defauts, "parquet")
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
    par_nom = {colonne.nom: tableau for colonne, tableau in zip(contrat.colonnes, colonnes, strict=True)}
    ecrites = []
    for nom, colonne in _colonnes_du_fichier(contrat, demandes):
        tableau = pa.array([VALEUR_COLONNE_INCONNUE] * len(lignes), type=pa.string()) if colonne is None else par_nom[nom]
        ecrites.append((nom, tableau))
    schema = pa.schema([(nom, tableau.type) for nom, tableau in ecrites])
    pq.write_table(pa.Table.from_arrays([tableau for _, tableau in ecrites], schema=schema), chemin)
    if DefautDeFichier.FICHIER_ILLISIBLE in demandes:
        octets = Path(chemin).read_bytes()
        Path(chemin).write_bytes(octets[: len(octets) // 2])      # le pied du fichier, qui porte son schéma, est perdu


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
