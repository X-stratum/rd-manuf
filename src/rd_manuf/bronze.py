"""Bronze : la copie fidèle des lignes reçues, toutes en texte, avec de quoi remonter jusqu'à la livraison.

Bronze ne juge pas et ne convertit rien : une ligne invalide y entre comme les autres, c'est le silver qui la mettra en quarantaine.
Il garde les lignes bien formées des livraisons ACCEPTEE, ACCEPTEE_AVEC_REJETS et BLOQUEE : une livraison bloquée pourra être retraitée
après correction, il faut donc la conserver. N'y entrent ni une livraison REFUSEE (le registre en garde la trace, et le fichier reste à la
source), ni un rejeu (le contenu est déjà là : l'ingérer deux fois le doublerait).

Les lignes coupées d'un CSV, qu'on ne peut pas ranger dans des colonnes, sont conservées à part, avec leur numéro de ligne dans le
fichier : elles comptent parmi les rejets qui font le statut de la livraison, et le silver en a besoin pour la rejuger comme la réception.

Ce module construit des lignes ; il n'écrit nulle part (l'écriture dans une table viendra avec le déploiement). La décision vient du
registre : c'est sa ligne qui dit si la livraison entre, d'où elle vient et quand elle est arrivée.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rd_manuf.contrat import Contrat, StatutLivraison
from rd_manuf.lecture import FichierLu
from rd_manuf.registre import LigneRegistre

STATUTS_BRONZE = frozenset({
    StatutLivraison.ACCEPTEE.value,
    StatutLivraison.ACCEPTEE_AVEC_REJETS.value,
    StatutLivraison.BLOQUEE.value,
})


@dataclass(frozen=True)
class LigneBronze:
    livraison_id: str                          # le nom du fichier, comme dans le registre
    heure_arrivee: datetime                    # en UTC ; avec `livraison_id`, distingue un fichier bloqué de sa version corrigée
    numero_ligne: int                          # RANG parmi les lignes bien formées, à partir de 1 : pas le numéro de ligne du fichier
    valeurs: dict[str, str | None]             # une entrée par colonne du contrat, dans son ordre ; None si la valeur est absente


@dataclass(frozen=True)
class LigneBronzeCoupee:
    livraison_id: str                          # le nom du fichier, comme dans le registre
    heure_arrivee: datetime                    # en UTC, comme pour `LigneBronze`
    numero_fichier: int                        # numéro de ligne relevé par le lecteur ; la dernière si multiligne
    champs: tuple[str, ...]                    # les valeurs brutes lues, telles que reçues : on ne sait pas à quelle colonne chacune va


def _entre_au_bronze(ligne_registre: LigneRegistre) -> bool:
    """La règle d'entrée, écrite une seule fois : c'est le registre qui décide, pour les lignes bien formées comme pour les coupées."""
    return ligne_registre.statut in STATUTS_BRONZE


def _verifier_la_lecture(ligne_registre: LigneRegistre, fichier: FichierLu) -> None:
    """Le fichier lu doit compter autant de lignes (bien formées et coupées) que le registre en a reçu.

    Sans cette garde, un silver qui rejuge le bronze pourrait travailler sur une livraison incomplète et décider autrement que la réception,
    sans que rien ne le signale. C'est aussi ce qui lie les deux fonctions de ce module : elles doivent recevoir la MÊME lecture.
    """
    lues = len(fichier.lignes) + len(fichier.lignes_malformees)
    if lues != ligne_registre.nb_recues:
        raise ValueError(
            f"Le fichier lu compte {lues} lignes (bien formées et coupées) mais le registre en a reçu {ligne_registre.nb_recues} pour la "
            f"livraison {ligne_registre.livraison_id} : ce n'est pas son fichier, ou des lignes se sont perdues à la lecture."
        )


def _en_texte(valeur: Any) -> str | None:
    return None if valeur is None else str(valeur)      # un CSV est déjà du texte (même vide) ; un Parquet est copié en texte


def construire_lignes_bronze(contrat: Contrat, ligne_registre: LigneRegistre, fichier: FichierLu) -> list[LigneBronze]:
    """Les lignes bronze d'une livraison ; une liste vide si le registre dit qu'elle n'entre pas.

    `fichier` est la lecture du fichier de cette livraison, déjà faite : ce module ne lit rien. Les lignes malformées d'un CSV n'y sont pas
    rangées : une ligne coupée ne dit pas à quelle colonne appartient chaque valeur. `construire_lignes_coupees` les conserve à part.
    """
    if not _entre_au_bronze(ligne_registre):
        return []
    _verifier_la_lecture(ligne_registre, fichier)
    noms = [colonne.nom for colonne in contrat.colonnes]
    if sorted(fichier.colonnes) != sorted(noms):
        raise ValueError(
            f"Les colonnes du fichier ({', '.join(fichier.colonnes)}) ne sont pas celles du contrat ({', '.join(noms)}) : "
            f"ce fichier n'est pas celui de la livraison {ligne_registre.livraison_id}, ou sa structure n'a pas été contrôlée."
        )
    return [
        LigneBronze(
            livraison_id=ligne_registre.livraison_id,
            heure_arrivee=ligne_registre.heure_arrivee,
            numero_ligne=rang,
            valeurs={colonne.nom: _en_texte(ligne[colonne.nom]) for colonne in contrat.colonnes},
        )
        for rang, ligne in enumerate(fichier.lignes, start=1)
    ]


def construire_lignes_coupees(ligne_registre: LigneRegistre, fichier: FichierLu) -> list[LigneBronzeCoupee]:
    """Les lignes coupées d'une livraison, conservées à part ; une liste vide si le registre dit qu'elle n'entre pas.

    Même règle d'entrée que `construire_lignes_bronze` (`STATUTS_BRONZE`) : c'est le registre qui décide. Ces lignes comptent parmi les
    rejets qui font le statut de la livraison : sans elles, le silver ne pourrait pas rejuger la livraison comme la réception.
    """
    if not _entre_au_bronze(ligne_registre):
        return []
    _verifier_la_lecture(ligne_registre, fichier)
    return [
        LigneBronzeCoupee(
            livraison_id=ligne_registre.livraison_id,
            heure_arrivee=ligne_registre.heure_arrivee,
            numero_fichier=coupee.numero,
            champs=coupee.champs,
        )
        for coupee in fichier.lignes_malformees
    ]
