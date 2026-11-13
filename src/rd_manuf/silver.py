"""Silver d'une livraison : les lignes valides, typées, et la quarantaine des lignes rejetées.

Le silver REJUGE le texte du bronze avec le contrat (`valider_livraison`, comme la réception) : une seule source de vérité pour les règles.
Il garantit de décider comme la réception : pour la MÊME version de contrat que le registre, le statut recalculé doit être celui du
registre, sinon une erreur claire (le silver ne doit jamais intégrer ce que la réception a bloqué). Avec une AUTRE version, c'est un
retraitement : le statut peut changer, et chaque ligne porte la version qui l'a jugée.

Entrent : les livraisons acceptées, acceptées avec rejets et bloquées (même règle que le bronze). Une livraison bloquée ne rend RIEN, ni
lignes ni quarantaine : elle est bloquée en entier, et le diagnostic est au registre. Un refus et un rejeu non plus. La quarantaine ne
sert donc qu'aux livraisons acceptées avec rejets, lignes coupées comprises. Un doublon exact est ignoré : il reste au bronze.

Un lot est identifié par son PAYS, son USINE et son `id_lot` : deux usines numérotent leurs lots chacune de leur côté, et comparer
l'`id_lot` seul les ferait entrer en conflit ou, pire, ignorerait en silence la seconde quand le contenu est le même. La ligne de
production n'en fait pas partie : une usine numérote les lots de toutes ses lignes, et un conflit entre deux de ses lignes reste visible.

Un lot déjà intégré par une livraison PLUS ANCIENNE (arrivée avant, selon le registre ; à heure égale, le nom départage) suit la même
règle : la première arrivée fait foi. Même contenu : ignorée ; contenu différent : quarantaine `CLE_EN_CONFLIT`, avec la livraison d'origine
nommée. Cela ne change NI le statut NI le seuil de rejet : la réception ne connaît que la livraison en cours, et le silver ne doit jamais
décider autrement qu'elle.

Ce module construit des lignes ; il n'écrit nulle part (l'écriture dans une table viendra avec le déploiement).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee
from rd_manuf.contrat import (
    CLE_NUMERO_LIGNE,
    CodeErreur,
    Contrat,
    LigneMalformee,
    StatutLivraison,
    Violation,
    valeurs_normalisees,
    valider_livraison,
)
from rd_manuf.registre import LigneRegistre


@dataclass(frozen=True)
class LigneSilver:
    livraison_id: str                          # le nom du fichier, comme au registre et au bronze
    heure_arrivee: datetime                    # en UTC
    pays: str                                  # d'où vient la livraison, d'après son nom : avec l'usine, il identifie le lot
    usine: str
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


def _cle(contrat: Contrat, valeurs: dict[str, Any]) -> tuple[str, ...]:
    """La clé d'unicité d'une ligne, calculée comme `valider_livraison` le fait au sein d'une livraison."""
    return tuple(str(valeurs[nom]) for nom in contrat.cle_primaire)


def _lot(pays: str, usine: str, cle: tuple[str, ...]) -> tuple[str, str, tuple[str, ...]]:
    """L'identité d'un lot : son pays, son usine et sa clé. Deux lots de pays ou d'usines différents ne se comparent jamais."""
    return (pays, usine, cle)


def _lignes_plus_anciennes(
    contrat: Contrat, deja_integrees: Sequence[LigneSilver], livraison_id: str, arrivee: datetime
) -> dict[tuple[str, str, tuple[str, ...]], LigneSilver]:
    """Par lot, la ligne de la livraison arrivée la PREMIÈRE, parmi celles qui précèdent la livraison courante.

    L'ordre est `(heure d'arrivée, nom de la livraison)` : deux livraisons arrivées à la même heure sont départagées par leur nom, sinon
    chacune ignorerait l'autre. La livraison courante est exclue par son NOM (retraitée, elle ne se compare pas à ses propres lignes).
    """
    anterieures: dict[tuple[str, str, tuple[str, ...]], LigneSilver] = {}
    courante = (arrivee, livraison_id)
    for ligne in sorted(deja_integrees, key=lambda ligne: (ligne.heure_arrivee, ligne.livraison_id)):
        if ligne.livraison_id != livraison_id and (ligne.heure_arrivee, ligne.livraison_id) < courante:
            anterieures.setdefault(_lot(ligne.pays, ligne.usine, _cle(contrat, ligne.valeurs)), ligne)
    return anterieures


def construire_silver(
    contrat: Contrat,
    ligne_registre: LigneRegistre,
    lignes_bronze: Sequence[LigneBronze],
    lignes_coupees: Sequence[LigneBronzeCoupee],
    deja_integrees: Sequence[LigneSilver] = (),
) -> ResultatSilver:
    """Rejuge la livraison depuis le bronze ; vide si le registre dit qu'elle n'entre pas ou si elle est bloquée.

    Lève `ValueError` si, avec la même version de contrat que le registre, le statut recalculé n'est pas celui du registre, ou si, quelle
    que soit la version, les lignes reçues (bien formées et coupées) ne font pas le compte du registre : c'est le signe que le silver ne
    reçoit pas tout ce qui est arrivé, et il ne doit alors rien intégrer. Lève aussi `ValueError` si le registre ne donne pas le pays et
    l'usine d'une livraison qui entre : ils identifient ses lots.

    `deja_integrees` : les lignes silver des livraisons déjà intégrées (lues dans la table plus tard) ; seules comptent celles arrivées
    AVANT cette livraison (heure d'arrivée, puis nom), donc un retraitement ne voit pas les suivantes et une livraison ne se compare pas à
    elle-même, quelle que soit l'heure avec laquelle elle est retraitée.
    """
    if ligne_registre.statut not in STATUTS_BRONZE:
        return ResultatSilver(lignes=[], quarantaine=[])
    pays, usine = ligne_registre.pays, ligne_registre.usine
    if pays is None or usine is None:
        raise ValueError(
            f"Le silver ne peut pas intégrer la livraison {ligne_registre.livraison_id} : le registre ne donne pas son pays et son usine "
            f"(pays={pays!r}, usine={usine!r}), qui font partie de l'identité d'un lot."
        )

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

    anterieures = _lignes_plus_anciennes(contrat, deja_integrees, ligne_registre.livraison_id, ligne_registre.heure_arrivee)
    lignes = []
    for texte in jugement.lignes_valides:
        bronze = au_bronze[id(texte)]
        valeurs = valeurs_normalisees(contrat, texte)
        cle = _cle(contrat, valeurs)
        origine = anterieures.get(_lot(pays, usine, cle))
        if origine is None:
            lignes.append(LigneSilver(pays=pays, usine=usine, numero_ligne=bronze.numero_ligne, valeurs=valeurs, **reference))
        elif origine.valeurs != valeurs:                                     # même clé, contenu différent : on ne sait pas lequel est juste
            message = (f"clé primaire {list(contrat.cle_primaire)} déjà intégrée par la livraison {origine.livraison_id} avec un contenu "
                       f"différent : {', '.join(cle)}")
            conflit = Violation(CodeErreur.CLE_EN_CONFLIT, contrat.cle_primaire, message)
            quarantaine.append(LigneQuarantaine(numero_ligne=bronze.numero_ligne, numero_fichier=None, contenu=dict(bronze.valeurs),
                                                violations=(conflit,), **reference))
        # même clé, même contenu : déjà là, ni silver ni quarantaine (comme un doublon exact dans une livraison)
    return ResultatSilver(lignes=lignes, quarantaine=quarantaine)
