"""L'INGESTION INCRÉMENTALE : parmi les fichiers du dossier d'arrivée, ne traiter que ceux que le registre n'a pas déjà vus.

Sans elle, relancer le traitement d'un dossier repasserait TOUS ses fichiers : chacun deviendrait un rejeu, avec sa ligne de registre et ses
alertes, à chaque lancement. Avec elle, un fichier est traité une fois ; un lancement de plus ne fait rien de plus.

« DÉJÀ VU » : un fichier dont le registre a une ligne de même nom ET de même contenu (l'empreinte), quel que soit le statut :
une livraison refusée ou bloquée qu'on ne corrige pas ne se rejuge pas à chaque lancement. Le même nom avec un AUTRE contenu est nouveau :
un renvoi corrigé d'une livraison refusée ou bloquée, ou un conflit avec une livraison intégrée, que `passer` enregistre une fois. Un nom
invalide, refusé sans que le fichier soit lu (donc sans empreinte), est vu par son seul nom. Le contenu d'un fichier n'est lu pour le
comparer que si son nom est déjà au registre : un dossier qui grossit ne coûte pas une lecture de chaque fichier à chaque lancement.

Le dossier est ordinaire : sur Databricks, un volume se lit comme un chemin. Seuls ses fichiers directs comptent ; les sous-dossiers
et les fichiers dont le nom commence par un point (un dépôt en cours) sont ignorés.

L'ORDRE est celui des noms, qui est celui du classement du silver `(heure d'arrivée, nom)` quand tous les fichiers du lot ont la même
heure d'arrivée. Une seule heure par lancement : une panne arrête le lot (rien n'est avalé), ce qui est écrit reste écrit, et relancer avec
LA MÊME heure reprend sans rien dupliquer, comme pour `passer`. Chaque fichier passe par `passer` : la séquence « lire, traiter, écrire »
n'est écrite qu'à un endroit (un test le garde).

Ce que ce module ne fait pas : il n'est pas un programme à lancer (le lancement par un job viendra avec l'orchestration), et il ne regarde
pas si un fichier est encore en train d'être déposé : l'émetteur doit le déposer sous un nom caché puis le renommer.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rd_manuf.alertes import Catalogue
from rd_manuf.contrat import Contrat
from rd_manuf.ecriture import EtatMagasin, Magasin
from rd_manuf.passage import ResultatPassage, passer
from rd_manuf.registre import LigneRegistre, empreinte_du_fichier
from rd_manuf.silver import LigneSilver


@dataclass(frozen=True)
class Selection:
    a_traiter: tuple[Path, ...]                 # les fichiers nouveaux, dans l'ordre des noms
    deja_vus: tuple[Path, ...]                  # les fichiers que le registre a déjà vus, sous ce nom et avec ce contenu


@dataclass(frozen=True)
class ResultatIngestion:
    selection: Selection
    passages: tuple[ResultatPassage, ...]       # un par fichier nouveau, dans l'ordre où ils ont été traités
    heure_arrivee: datetime                     # celle de tout le lot : à redonner pour reprendre
    ecrit: bool                                 # vrai seulement si les résultats ont été écrits dans le magasin


def fichiers_du_dossier(dossier: str | Path) -> list[Path]:
    """Les fichiers directs du dossier, dans l'ordre des noms ; sous-dossiers et fichiers cachés (point initial) exclus."""
    return sorted((c for c in Path(dossier).iterdir() if c.is_file() and not c.name.startswith(".")), key=lambda c: c.name)


def _deja_vu(chemin: Path, lignes_du_nom: list[LigneRegistre]) -> bool:
    if not lignes_du_nom:
        return False                                                         # nom inconnu : nouveau, sans rien lire pour le décider
    if any(ligne.empreinte is None for ligne in lignes_du_nom):
        return True                                                          # nom refusé sans lecture : seul un autre nom le corrige
    empreinte = empreinte_du_fichier(chemin)
    return any(ligne.empreinte == empreinte for ligne in lignes_du_nom)


def selectionner(dossier: str | Path, registre: list[LigneRegistre]) -> Selection:
    """Sépare les fichiers du dossier en nouveaux et déjà vus, d'après le registre. Ne lit que ce qu'il faut pour comparer."""
    par_nom: dict[str, list[LigneRegistre]] = {}
    for ligne in registre:
        par_nom.setdefault(ligne.livraison_id, []).append(ligne)
    a_traiter: list[Path] = []
    deja_vus: list[Path] = []
    for chemin in fichiers_du_dossier(dossier):
        (deja_vus if _deja_vu(chemin, par_nom.get(chemin.name, [])) else a_traiter).append(chemin)
    return Selection(tuple(a_traiter), tuple(deja_vus))


def ingerer(
    contrat: Contrat, alertes: Catalogue, dossier: str | Path, magasin: Magasin, heure_arrivee: datetime, *, ecrire: bool,
    lire: Callable[[], tuple[list[LigneRegistre], list[LigneSilver]]] | None = None,
) -> ResultatIngestion:
    """Traite, dans l'ordre des noms, chaque fichier du dossier que le registre n'a pas déjà vu ; écrit seulement si `ecrire`.

    Un dossier introuvable lève `FileNotFoundError`, une heure sans fuseau `ValueError` (au premier fichier à traiter) : des erreurs de
    l'appelant. Une panne d'un passage arrête le lot et s'échappe telle quelle. `lire` donne la lecture de l'état à la place de celle du
    magasin, comme pour `passer` ; en simulation, rien n'est écrit : chaque fichier est jugé sur l'état des tables, sans voir les fichiers
    précédents du même lot, et le registre ne change pas, donc tout fichier nouveau le reste d'un lancement à l'autre.
    """
    registre = lire()[0] if lire is not None else EtatMagasin(contrat, magasin).registre
    selection = selectionner(dossier, registre)
    passages = tuple(passer(contrat, alertes, chemin, magasin, heure_arrivee, ecrire=ecrire, lire=lire) for chemin in selection.a_traiter)
    return ResultatIngestion(selection, passages, heure_arrivee, ecrire)
