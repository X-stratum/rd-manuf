"""Registre des livraisons : UNE ligne par livraison reçue, avec sa décision, d'où et de quand elle vient, et quand elle est arrivée.

Ce module construit les lignes ; il n'écrit nulle part (l'écriture dans une table viendra avec le déploiement).
L'heure d'arrivée est PASSÉE EN PARAMÈTRE, jamais lue sur l'horloge : les tests sont reproductibles et une règle de plausibilité des dates
(un fichier daté du futur) pourra s'appuyer dessus.

Un REJEU est un fichier déjà intégré (accepté, avec ou sans rejets) qui arrive de nouveau avec le MÊME CONTENU : il est ignoré et le
registre en garde la trace. Avec un contenu DIFFÉRENT sous le même nom, c'est un CONFLIT : refusé avec alerte, jamais ignoré en silence
(des données corrigées seraient perdues). Un fichier refusé ou bloqué n'est pas intégré : il peut être renvoyé sous le même nom, corrigé.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rd_manuf.contrat import Contrat, StatutLivraison
from rd_manuf.nom_fichier import NomFichier, NomFichierInvalide, lire_nom_fichier
from rd_manuf.reception import Reception, receptionner_fichier, refuser_conflit

# Pas un `StatutLivraison` : aucune décision n'est prise sur un rejeu, la première livraison reste valable.
STATUT_REJEU = "REJEU_IGNORE"
STATUTS_INTEGRES = frozenset({StatutLivraison.ACCEPTEE.value, StatutLivraison.ACCEPTEE_AVEC_REJETS.value})


@dataclass(frozen=True)
class LigneRegistre:
    livraison_id: str                          # le nom du fichier
    statut: str                                # une valeur de `StatutLivraison`, ou `STATUT_REJEU`
    version_contrat: str
    nb_recues: int
    nb_valides: int
    nb_rejetees: int
    codes_refus: tuple[str, ...]               # motifs de structure d'un refus, dans l'ordre des codes ; vide sinon
    pays: str | None                           # None si le nom du fichier est invalide
    usine: str | None
    horodatage_fichier: datetime | None        # l'instant écrit dans le nom, en UTC
    heure_arrivee: datetime                    # l'instant où la livraison est arrivée, en UTC
    empreinte: str | None                      # SHA-256 du contenu du fichier ; None si le nom est invalide (fichier non lu)
    rejeu_de: str | None                       # identifiant de la livraison déjà intégrée dont celle-ci est un rejeu


def _en_utc(heure: datetime) -> datetime:
    if heure.utcoffset() is None:
        raise ValueError("L'heure d'arrivée doit avoir un fuseau (UTC attendu) : une heure sans fuseau ne se compare pas.")
    return heure.astimezone(UTC)


def construire_ligne_registre(reception: Reception, heure_arrivee: datetime, empreinte: str | None = None) -> LigneRegistre:
    nom = reception.nom_fichier
    return LigneRegistre(
        livraison_id=reception.livraison_id,
        statut=reception.statut.value,
        version_contrat=reception.version_contrat,
        nb_recues=reception.nb_recues,
        nb_valides=reception.nb_valides,
        nb_rejetees=reception.nb_rejetees,
        codes_refus=tuple(probleme.code.value for probleme in reception.problemes_structure),
        pays=nom.pays if nom else None,
        usine=nom.usine if nom else None,
        horodatage_fichier=nom.horodatage if nom else None,
        heure_arrivee=_en_utc(heure_arrivee),
        empreinte=empreinte,
        rejeu_de=None,
    )


def _livraison_deja_integree(registre: Sequence[LigneRegistre], nom: NomFichier) -> LigneRegistre | None:
    identite = (nom.pays, nom.usine, nom.horodatage)
    integrees = (ligne for ligne in registre if ligne.statut in STATUTS_INTEGRES)
    return next((ligne for ligne in integrees if (ligne.pays, ligne.usine, ligne.horodatage_fichier) == identite), None)


def _empreinte(chemin: Path) -> str:
    with chemin.open("rb") as fichier:
        return hashlib.file_digest(fichier, "sha256").hexdigest()


def _ligne_de_rejeu(chemin: Path, nom: NomFichier, original: LigneRegistre, heure: datetime) -> LigneRegistre:
    return LigneRegistre(
        livraison_id=chemin.name,
        statut=STATUT_REJEU,
        version_contrat=original.version_contrat,
        nb_recues=0,
        nb_valides=0,
        nb_rejetees=0,
        codes_refus=(),
        pays=nom.pays,
        usine=nom.usine,
        horodatage_fichier=nom.horodatage,
        heure_arrivee=heure,
        empreinte=original.empreinte,
        rejeu_de=original.livraison_id,
    )


def traiter_fichier(
    contrat: Contrat,
    chemin: str | Path,
    registre: Sequence[LigneRegistre],
    heure_arrivee: datetime,
) -> tuple[Reception | None, LigneRegistre]:
    """Le point d'entrée complet : rejeu ou conflit éventuels, sinon réception du fichier, puis la ligne de registre correspondante.

    Rend la réception (None pour un rejeu : rien n'est relu ni rejugé) et la ligne à ajouter au registre.
    """
    chemin = Path(chemin)
    heure = _en_utc(heure_arrivee)                    # échoue tout de suite si l'heure est « naïve »
    try:
        nom = lire_nom_fichier(chemin.name)
    except NomFichierInvalide:
        reception = receptionner_fichier(contrat, chemin)             # refuse ce nom, avec son motif, sans lire le fichier
        return reception, construire_ligne_registre(reception, heure)

    empreinte = _empreinte(chemin)
    original = _livraison_deja_integree(registre, nom)
    if original is not None and original.empreinte == empreinte:
        return None, _ligne_de_rejeu(chemin, nom, original, heure)
    if original is not None:
        reception = refuser_conflit(contrat, chemin.name, nom, original.livraison_id)
    else:
        reception = receptionner_fichier(contrat, chemin)
    return reception, construire_ligne_registre(reception, heure, empreinte)
