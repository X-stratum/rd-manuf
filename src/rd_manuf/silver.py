"""Silver d'une livraison : les lignes valides, typées, et la quarantaine des lignes rejetées.

Le silver REJUGE le texte du bronze avec le contrat (`valider_livraison`, comme la réception) : une seule source de vérité pour les règles.
Il garantit de décider comme la réception : pour la MÊME version de contrat que le registre, le statut recalculé doit être celui du
registre, sinon une erreur claire (le silver ne doit jamais intégrer ce que la réception a bloqué). Avec une AUTRE version, c'est un
retraitement : le statut peut changer, et chaque ligne porte la version qui l'a jugée.

Entrent : les livraisons acceptées, acceptées avec rejets et bloquées (même règle que le bronze). Une livraison bloquée ne rend RIEN, ni
lignes ni quarantaine : elle est bloquée en entier, et le diagnostic est au registre. Un refus et un rejeu non plus. La quarantaine ne
sert donc qu'aux livraisons acceptées avec rejets, lignes coupées comprises. Un doublon exact est ignoré : il reste au bronze.

Ce module construit des lignes ; il n'écrit nulle part (l'écriture dans une table viendra avec le déploiement).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee
from rd_manuf.contrat import CLE_NUMERO_LIGNE, Contrat, LigneMalformee, StatutLivraison, Violation, valeurs_normalisees, valider_livraison
from rd_manuf.registre import LigneRegistre


@dataclass(frozen=True)
class LigneSilver:
    livraison_id: str                          # le nom du fichier, comme au registre et au bronze
    heure_arrivee: datetime                    # en UTC
    numero_ligne: int                          # le RANG de la ligne au bronze (parmi les lignes bien formées)
    version_contrat: str                       # la version du contrat qui a jugé la ligne
    valeurs: dict[str, Any]                    # TYPÉES : entier, décimal, date ; None si la valeur est absente


@dataclass(frozen=True)
class LigneQuarantaine:
    livraison_id: str
    heure_arrivee: datetime
    version_contrat: str
    numero_ligne: int | None                   # le rang au bronze, pour une ligne bien formée ; None pour une ligne coupée
    numero_fichier: int | None                 # le numéro de ligne du fichier, pour une ligne coupée ; None sinon
    contenu: dict[str, str | None] | tuple[str, ...]   # tel que reçu : les valeurs en texte, ou les champs bruts d'une ligne coupée
    violations: tuple[Violation, ...]          # chaque code et les colonnes à corriger


@dataclass(frozen=True)
class ResultatSilver:
    lignes: list[LigneSilver]
    quarantaine: list[LigneQuarantaine]


def construire_silver(
    contrat: Contrat,
    ligne_registre: LigneRegistre,
    lignes_bronze: Sequence[LigneBronze],
    lignes_coupees: Sequence[LigneBronzeCoupee],
) -> ResultatSilver:
    """Rejuge la livraison depuis le bronze ; vide si le registre dit qu'elle n'entre pas ou si elle est bloquée.

    Lève `ValueError` si, avec la même version de contrat que le registre, le statut recalculé n'est pas celui du registre, ou si, quelle
    que soit la version, les lignes reçues (bien formées et coupées) ne font pas le compte du registre : c'est le signe que le silver ne
    reçoit pas tout ce qui est arrivé, et il ne doit alors rien intégrer.
    """
    if ligne_registre.statut not in STATUTS_BRONZE:
        return ResultatSilver(lignes=[], quarantaine=[])

    textes = [ligne.valeurs for ligne in lignes_bronze]
    coupees = [LigneMalformee(numero=coupee.numero_fichier, champs=coupee.champs) for coupee in lignes_coupees]
    jugement = valider_livraison(contrat, textes, coupees)

    if contrat.version == ligne_registre.version_contrat and jugement.statut.value != ligne_registre.statut:
        raise ValueError(
            f"Le silver recalcule le statut {jugement.statut.value} pour la livraison {ligne_registre.livraison_id} sous le contrat "
            f"{contrat.version}, mais le registre a le statut {ligne_registre.statut} sous la même version : le silver ne reçoit pas tout "
            "ce qui est arrivé."
        )
    lues = len(lignes_bronze) + len(lignes_coupees)
    if lues != ligne_registre.nb_recues:
        raise ValueError(
            f"Le silver reçoit {lues} lignes (bien formées et coupées) pour la livraison {ligne_registre.livraison_id}, mais le registre "
            f"a reçu {ligne_registre.nb_recues} lignes : une livraison incomplète ne doit pas être intégrée."
        )
    if jugement.statut == StatutLivraison.BLOQUEE:
        return ResultatSilver(lignes=[], quarantaine=[])

    au_bronze = {id(ligne.valeurs): ligne for ligne in lignes_bronze}       # valider_livraison rend les MÊMES dictionnaires qu'il a reçus
    coupee_par_numero = {coupee.numero_fichier: coupee for coupee in lignes_coupees}
    reference = {
        "livraison_id": ligne_registre.livraison_id,
        "heure_arrivee": ligne_registre.heure_arrivee,
        "version_contrat": contrat.version,
    }

    lignes = [
        LigneSilver(numero_ligne=au_bronze[id(texte)].numero_ligne, valeurs=valeurs_normalisees(contrat, texte), **reference)
        for texte in jugement.lignes_valides
    ]
    quarantaine = []
    for rejetee in jugement.lignes_rejetees:
        ligne = au_bronze.get(id(rejetee.ligne))                             # reconnue par son identité, pas par une clé de ses données
        if ligne is not None:
            quarantaine.append(LigneQuarantaine(numero_ligne=ligne.numero_ligne, numero_fichier=None, contenu=dict(ligne.valeurs),
                                                violations=rejetee.violations, **reference))
        else:                                                                # une ligne coupée, rejetée par `valider_livraison`
            numero = rejetee.ligne[CLE_NUMERO_LIGNE]
            quarantaine.append(LigneQuarantaine(numero_ligne=None, numero_fichier=numero, contenu=coupee_par_numero[numero].champs,
                                                violations=rejetee.violations, **reference))
    return ResultatSilver(lignes=lignes, quarantaine=quarantaine)
