"""Le RETRAITEMENT : rejuger une livraison déjà reçue sous une AUTRE version du contrat, et dire ce que ce nouveau jugement change.

Un contrat évolue (un seuil, une plage, une colonne) et les livraisons déjà intégrées ont été jugées sous l'ancienne version. Ce module
choisit celles à rejuger (`a_retraiter`) et rend, pour l'une d'elles, ce qu'un retraitement ajouterait (`retraiter`) : une nouvelle
ligne de registre, les lignes silver et la quarantaine de ce nouveau jugement, et un RECLASSEMENT quand le statut ou les comptes
changent.

Il ne refait rien de ce que le reste du code sait déjà : la réception juge des lignes lues (`receptionner_livraison`), le registre
fabrique sa ligne à partir d'une réception (`construire_ligne_registre`) et le silver rejuge le bronze avec un autre contrat
(`construire_silver`). Ici on les ENCHAÎNE sur ce que le bronze a gardé : le texte des lignes bien formées, et les lignes coupées. La
structure est rejugée aussi, depuis les colonnes que le bronze a gardées : un contrat qui exige une colonne absente du bronze REFUSE la
livraison (le fichier d'origine seul permettrait de la corriger, par un renvoi). Une livraison sans aucune ligne bien formée n'a gardé
aucun en-tête : on prend alors les colonnes du contrat, plutôt que de la refuser à tort pour « colonnes manquantes ».

LA LIVRAISON À RETRAITER est la décision EN VIGUEUR de chaque nom : sa dernière ligne de registre dont les lignes entrent au bronze
(acceptée, acceptée avec rejets, bloquée). Un refus, un rejeu ou un conflit plus récent ne la remplace pas, une livraison seulement
refusée n'a rien à rejuger. Elle est à retraiter si elle a été jugée sous une AUTRE version que celle du contrat donné.

LE NOUVEAU JUGEMENT porte la MÊME livraison, une nouvelle heure (celle du retraitement, avec fuseau, POSTÉRIEURE à l'arrivée) et la
nouvelle version : la clé du registre est `(livraison, heure)`, un retraitement à l'heure d'origine porterait la clé de l'ancien
jugement. Le silver compare la livraison aux lignes DÉJÀ intégrées à cette heure, comme pour toute livraison : une livraison bloquée
puis rejugée ne double donc pas des lots qu'une autre a intégrés entre-temps.

PLUSIEURS RETRAITEMENTS : un retraitement ne réécrit pas le bronze. La ligne qu'il ajoute porte l'heure du retraitement, mais les lignes
bronze gardent l'arrivée d'origine. Pour retraiter une livraison une seconde fois, `retrouver_arrivee_du_bronze` donne cette arrivée
(celle de la première ligne de même nom et de même contenu) et `retraiter` la prend en `arrivee_du_bronze` ; sans elle, les lignes
bronze doivent avoir l'arrivée de la ligne, donc celles d'une autre arrivée restent refusées. Le même nom avec un AUTRE contenu (un
renvoi corrigé) a ses propres lignes, donc sa propre arrivée.

CE QUE CE MODULE NE FAIT PAS : il n'écrit rien, il ne prévient personne (le reclassement est une donnée, comme les constats de
surveillance), et il ne dit pas comment le nouveau jugement REMPLACE l'ancien dans l'état : sans règle de remplacement, ajouter ses
lignes à côté des anciennes ferait compter deux fois la livraison. Cette règle (la ligne « en vigueur » d'une livraison, l'historique
des versions) appartient à l'évolution du contrat.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee
from rd_manuf.contrat import Contrat, LigneMalformee
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.reception import receptionner_livraison
from rd_manuf.registre import LigneRegistre, construire_ligne_registre, en_utc
from rd_manuf.silver import LigneSilver, ResultatSilver, construire_silver

LIVRAISON_RECLASSEE = "LIVRAISON_RECLASSEE"


@dataclass(frozen=True)
class Reclassement:
    code: str
    livraison_id: str
    statut_avant: str
    statut_apres: str
    version_avant: str
    version_apres: str
    nb_valides_avant: int                       # lignes valides au registre, avant et après : une bloquée ou une refusée n'en a aucune
    nb_valides_apres: int
    nb_rejetees_avant: int
    nb_rejetees_apres: int


@dataclass(frozen=True)
class ResultatRetraitement:
    """Tout ce qu'un retraitement ajouterait : rien n'est écrit, et rien ne dit encore comment il remplace l'ancien jugement."""

    ligne_registre: LigneRegistre               # la même livraison, avec la nouvelle heure, version, statut et les nouveaux comptes
    silver: ResultatSilver                      # les lignes valides et la quarantaine du nouveau jugement, portant la nouvelle version
    reclassement: Reclassement | None           # vide si statut et comptes sont ceux d'avant : la version change, pas le sort


def a_retraiter(registre: Iterable[LigneRegistre], contrat: Contrat) -> list[LigneRegistre]:
    """Les décisions en vigueur jugées sous une autre version que celle de `contrat`, dans l'ordre d'arrivée puis du nom.
    Ne modifie pas ses entrées.

    En vigueur : pour chaque nom, la dernière ligne dont les lignes entrent au bronze ; à arrivée égale, la dernière du registre."""
    en_vigueur: dict[str, tuple[datetime, int, LigneRegistre]] = {}
    for rang, ligne in enumerate(registre):
        if ligne.statut not in STATUTS_BRONZE:
            continue
        courante = en_vigueur.get(ligne.livraison_id)
        if courante is None or (ligne.heure_arrivee, rang) >= courante[:2]:
            en_vigueur[ligne.livraison_id] = (ligne.heure_arrivee, rang, ligne)
    a_jour = [ligne for _, _, ligne in en_vigueur.values() if ligne.version_contrat != contrat.version]
    return sorted(a_jour, key=lambda ligne: (ligne.heure_arrivee, ligne.livraison_id))


def retrouver_arrivee_du_bronze(registre: Iterable[LigneRegistre], ligne: LigneRegistre) -> datetime:
    """L'arrivée dont le bronze porte les lignes de `ligne` : celle de la première ligne de même nom, de même contenu (même empreinte) et
    dont les lignes entrent au bronze.

    Un retraitement ne réécrit pas le bronze : la ligne qu'il ajoute porte l'heure du retraitement, mais ses lignes bronze gardent l'arrivée
    d'origine. Le même nom avec un AUTRE contenu (un renvoi corrigé) a ses propres lignes, donc sa propre arrivée. Ne modifie pas ses
    entrées."""
    memes = [autre.heure_arrivee for autre in registre
             if autre.livraison_id == ligne.livraison_id and autre.empreinte == ligne.empreinte and autre.statut in STATUTS_BRONZE]
    return min([ligne.heure_arrivee, *memes])


def _verifier_les_entrees(
    contrat: Contrat, ligne: LigneRegistre, bronze: Sequence[LigneBronze], coupees: Sequence[LigneBronzeCoupee], heure: datetime,
    arrivee_du_bronze: datetime,
) -> None:
    if ligne.statut not in STATUTS_BRONZE:
        raise ValueError(
            f"La livraison {ligne.livraison_id} est {ligne.statut} : ses lignes ne sont pas au bronze, il n'y a rien à rejuger."
        )
    if ligne.version_contrat == contrat.version:
        raise ValueError(
            f"La livraison {ligne.livraison_id} est déjà jugée sous la version {contrat.version} du contrat : il n'y a rien à retraiter."
        )
    if heure <= ligne.heure_arrivee:
        raise ValueError(
            f"L'heure du retraitement ({heure:%Y-%m-%d %H:%M:%S} UTC) doit être postérieure à l'arrivée de la livraison "
            f"{ligne.livraison_id} ({ligne.heure_arrivee:%Y-%m-%d %H:%M:%S} UTC) : sinon elle porterait la clé de l'ancien jugement."
        )
    gardees: list[LigneBronze | LigneBronzeCoupee] = [*bronze, *coupees]
    for gardee in gardees:
        if gardee.livraison_id != ligne.livraison_id:
            raise ValueError(f"Une ligne du bronze est celle de la livraison {gardee.livraison_id}, pas de {ligne.livraison_id}.")
        if gardee.heure_arrivee != arrivee_du_bronze:
            raise ValueError(
                f"Une ligne du bronze n'est pas celle de l'arrivée {arrivee_du_bronze:%Y-%m-%d %H:%M:%S} UTC de la livraison "
                f"{ligne.livraison_id} (renvoyée sous le même nom, ses lignes sont celles d'une autre arrivée)."
            )
    if len(bronze) + len(coupees) != ligne.nb_recues:
        raise ValueError(
            f"Le bronze garde {len(bronze) + len(coupees)} lignes de la livraison {ligne.livraison_id}, mais le registre en a reçu "
            f"{ligne.nb_recues} : une livraison incomplète ne se retraite pas."
        )


def retraiter(
    contrat: Contrat, ligne_registre: LigneRegistre, lignes_bronze: Sequence[LigneBronze], lignes_coupees: Sequence[LigneBronzeCoupee],
    deja_integrees: Sequence[LigneSilver], heure_arrivee: datetime, *, arrivee_du_bronze: datetime | None = None,
) -> ResultatRetraitement:
    """Rejuge la livraison de `ligne_registre` sous `contrat`, depuis ses lignes bronze et ses lignes coupées, à l'heure `heure_arrivee`.

    `arrivee_du_bronze` : l'arrivée dont les lignes bronze portent la clé, quand elle n'est pas celle de `ligne_registre` : une livraison
    déjà retraitée a la ligne et l'heure de son dernier retraitement, mais son bronze d'origine (voir `retrouver_arrivee_du_bronze`). Par
    défaut, l'arrivée de la ligne : les lignes d'un renvoi corrigé ne sont jamais prises pour celles de la première version.

    `deja_integrees` : les lignes silver des livraisons déjà intégrées, comme pour `construire_silver` (la livraison ne se compare pas à ses
    propres lignes). Lève `ValueError` si la livraison est déjà jugée sous cette version ou ne vient pas au bronze, si l'heure n'a pas de
    fuseau ou n'est pas postérieure à son arrivée, ou si les lignes données ne sont pas, toutes, celles de cette livraison et de cette
    arrivée, ou ne font pas le compte du registre."""
    heure = en_utc(heure_arrivee)
    arrivee_des_lignes = ligne_registre.heure_arrivee if arrivee_du_bronze is None else arrivee_du_bronze
    _verifier_les_entrees(contrat, ligne_registre, lignes_bronze, lignes_coupees, heure, arrivee_des_lignes)
    colonnes = list(lignes_bronze[0].valeurs) if lignes_bronze else [colonne.nom for colonne in contrat.colonnes]
    malformees = [LigneMalformee(numero=coupee.numero_fichier, champs=coupee.champs) for coupee in lignes_coupees]
    textes = [ligne.valeurs for ligne in lignes_bronze]
    reception = receptionner_livraison(contrat, ligne_registre.livraison_id, colonnes, textes, malformees)
    nouvelle = construire_ligne_registre(
        replace(reception, nom_fichier=lire_nom_fichier(ligne_registre.livraison_id)), heure, ligne_registre.empreinte
    )
    silver = construire_silver(contrat, nouvelle, lignes_bronze, lignes_coupees, deja_integrees)
    avant, apres = ligne_registre, nouvelle
    if (avant.statut, avant.nb_valides, avant.nb_rejetees) == (apres.statut, apres.nb_valides, apres.nb_rejetees):
        return ResultatRetraitement(nouvelle, silver, None)
    reclassement = Reclassement(
        LIVRAISON_RECLASSEE, avant.livraison_id, avant.statut, apres.statut, avant.version_contrat, apres.version_contrat,
        avant.nb_valides, apres.nb_valides, avant.nb_rejetees, apres.nb_rejetees,
    )
    return ResultatRetraitement(nouvelle, silver, reclassement)
