"""Le PASSAGE d'une livraison dans les tables : la traiter avec le pipeline, puis (seulement si on le demande) l'écrire.

Deux niveaux. `passer` est la logique : elle travaille sur un MAGASIN quelconque (le magasin en mémoire en test, le magasin SQL d'un
exécuteur dans le programme), lit l'état déjà écrit, traite le fichier, et l'écrit si `ecrire` est vrai. `principal` est le programme, à
lancer soi-même depuis la racine du dépôt ; il APPELLE `passer` et ne réécrit aucune de ses étapes (un test le garde) :

    python -m rd_manuf.passage --profil NOM_DU_PROFIL --entrepot IDENTIFIANT --fichier CHEMIN_DE_LA_LIVRAISON [--ecrire]

SIMULATION PAR DÉFAUT : sans `--ecrire`, il ne part que des lectures (`SELECT`) ; le programme dit la décision (statut, lignes, alertes) et
ne crée ni n'écrit rien. Avec `--ecrire`, il crée les schémas et les tables s'ils manquent PUIS lit l'état et écrit, les couches de données
d'abord et le registre en dernier (voir `ecriture.py`). Seul le catalogue playground est accepté : dev et prod sont refusés, avant toute
connexion ; ouvrir dev sera un choix explicite, dans une autre PR.

Le profil et l'identifiant de l'entrepôt viennent de la ligne de commande : le dépôt est public, rien n'est lu ailleurs que par le SDK.
La sortie va au terminal seulement et ne montre aucune valeur de ligne : des comptes et des codes.

L'HEURE D'ARRIVÉE est une option (`--heure-arrivee`, avec fuseau ; maintenant en UTC par défaut) et elle est toujours affichée : relancer
une livraison interrompue avec LA MÊME heure la reprend sans rien dupliquer (la clé du registre est le nom du fichier et cette heure).

Codes de sortie : 0 la livraison a été traitée, quel que soit son statut (une livraison refusée ou bloquée est un traitement correct, avec
ses alertes : une orchestration ne doit pas échouer parce qu'une usine a envoyé un mauvais fichier) ; 1 la création, la lecture, le
traitement ou l'écriture a échoué ; 2 le lancement est refusé (catalogue, fichier, contrat, heure, ou connexion) ; 130 interrompu
au clavier.
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rd_manuf.alertes import Catalogue, CatalogueInvalide, charger_catalogue
from rd_manuf.config import CATALOGUE_PAR_DEFAUT
from rd_manuf.contrat import Contrat, ContratInvalide, charger_contrat
from rd_manuf.ecriture import DecisionManquante, EtatMagasin, Magasin, MagasinSql, creer_tables
from rd_manuf.pipeline import ResultatLivraison, traiter_livraison
from rd_manuf.registre import LigneRegistre
from rd_manuf.silver import LigneSilver
from rd_manuf.sondage import (
    CAUSES_A_VERIFIER,
    CONTRAT_PAR_DEFAUT,
    DELAI_PAR_DEFAUT,
    SQL_CONNEXION,
    SdkAbsent,
    borne,
    delai_positif,
    duree,
    fabriquer_executeur_reel,
    verifier_la_presence,
)

ALERTES_PAR_DEFAUT = "contracts/alertes/catalogue.yaml"           # relatif à la racine du dépôt, d'où le programme se lance


# ==============================================================================
# 🧠 LA LOGIQUE : lire l'état, traiter, écrire
# ==============================================================================
@dataclass(frozen=True)
class ResultatPassage:
    resultat: ResultatLivraison                 # tout ce que la livraison ajoute à l'état
    heure_arrivee: datetime                     # celle qui a servi : à redonner pour reprendre
    ecrit: bool                                 # vrai seulement si le résultat a été écrit dans le magasin


def lire_l_etat(contrat: Contrat, magasin: Magasin) -> tuple[list[LigneRegistre], list[LigneSilver]]:
    """Le registre et les lignes silver déjà intégrées, tels que le magasin les garde : de quoi reconnaître un rejeu ou un conflit."""
    etat = EtatMagasin(contrat, magasin)
    return etat.registre, etat.lignes_silver


def ecrire_le_resultat(contrat: Contrat, magasin: Magasin, resultat: ResultatLivraison) -> None:
    """Écrit les couches de données d'abord et le registre en dernier ; rejouée avec la même heure d'arrivée, elle ne duplique rien."""
    EtatMagasin(contrat, magasin).appliquer(resultat)


def passer(
    contrat: Contrat, alertes: Catalogue, chemin: str | Path, magasin: Magasin, heure_arrivee: datetime, *, ecrire: bool,
    lire: Callable[[], tuple[list[LigneRegistre], list[LigneSilver]]] | None = None,
    jalon: Callable[[str, ResultatLivraison | None], None] | None = None,
) -> ResultatPassage:
    """Lit l'état, traite la livraison, et l'écrit si `ecrire`. Un fichier introuvable lève `FileNotFoundError`, une heure sans fuseau
    `ValueError` : ce sont des erreurs de l'appelant, pas des défauts de la livraison.

    C'est LA séquence : le programme l'appelle, il ne la réécrit pas (un test le garde). Deux options pour lui, sans effet par défaut :
    `lire` donne la lecture de l'état à la place de celle du magasin (la simulation part d'un état vide quand des tables manquent), et
    `jalon(nom, résultat)` est appelé AVANT chaque étape : « lecture », « traitement », puis « traite » (avec le résultat, avant toute
    écriture) et, seulement si `ecrire`, « ecriture » (avec le résultat). Une panne est donc celle de la dernière étape annoncée."""

    def annoncer(nom: str, resultat: ResultatLivraison | None = None) -> None:
        if jalon is not None:
            jalon(nom, resultat)

    annoncer("lecture")
    registre, deja_integrees = lire() if lire is not None else lire_l_etat(contrat, magasin)
    annoncer("traitement")
    resultat = traiter_livraison(contrat, alertes, chemin, registre, deja_integrees, heure_arrivee)
    annoncer("traite", resultat)
    if ecrire:
        annoncer("ecriture", resultat)
        ecrire_le_resultat(contrat, magasin, resultat)
    return ResultatPassage(resultat, heure_arrivee, ecrire)


# ==============================================================================
# 🚀 LE PROGRAMME
# ==============================================================================
def _lecteur_d_arguments() -> argparse.ArgumentParser:
    lecteur = argparse.ArgumentParser(
        prog="python -m rd_manuf.passage",
        description="Traite une livraison avec le pipeline. Sans --ecrire : simulation, seulement des lectures. Playground seulement.",
    )
    lecteur.add_argument("--profil", required=True, help="le nom du profil de connexion Databricks (jamais écrit dans le dépôt)")
    lecteur.add_argument("--entrepot", required=True, help="l'identifiant de l'entrepôt SQL à utiliser")
    lecteur.add_argument("--fichier", required=True, help="la livraison à traiter (CSV ou Parquet, au nom conforme)")
    lecteur.add_argument("--ecrire", action="store_true", help="créer les tables si besoin et écrire (sinon : simulation)")
    lecteur.add_argument("--heure-arrivee", default=None, help="l'heure d'arrivée, avec fuseau (2026-11-12T09:00:00Z) ; maintenant")
    lecteur.add_argument("--catalogue", default=CATALOGUE_PAR_DEFAUT, help=f"refusé s'il n'est pas le playground ({CATALOGUE_PAR_DEFAUT})")
    lecteur.add_argument("--contrat", default=CONTRAT_PAR_DEFAUT, help="le contrat de données, relatif au dossier courant")
    lecteur.add_argument("--alertes", default=ALERTES_PAR_DEFAUT, help="le catalogue d'alertes, relatif au dossier courant")
    lecteur.add_argument("--delai", type=delai_positif, default=DELAI_PAR_DEFAUT,
                         help=f"délai maximal de chaque requête, en secondes ({DELAI_PAR_DEFAUT:g} par défaut)")
    return lecteur


def _lire_l_heure(texte: str | None, maintenant: Callable[[], datetime]) -> datetime:
    """L'heure d'arrivée, en UTC : refusée sans fuseau (une heure sans fuseau est ambiguë) ou illisible."""
    if texte is None:
        instant = maintenant().replace(microsecond=0)         # tronquée : ce qui est affiché est exactement la clé de reprise
    else:
        try:
            instant = datetime.fromisoformat(texte)
        except ValueError:
            raise ValueError(f"« {borne(texte)} » n'est pas une date et heure ISO 8601 (exemple : 2026-11-12T09:00:00Z).") from None
    if instant.utcoffset() is None:
        raise ValueError(f"« {borne(str(texte))} » n'a pas de fuseau horaire : ajouter Z (UTC) ou un décalage (+01:00).")
    return instant.astimezone(UTC)


def _affichee(instant: datetime) -> str:
    """L'heure d'arrivée telle qu'elle s'affiche : à la seconde, microsecondes comprises SEULEMENT s'il y en a (elles sont dans la clé)."""
    forme = "%Y-%m-%d %H:%M:%S.%f" if instant.microsecond else "%Y-%m-%d %H:%M:%S"
    return instant.strftime(forme) + " UTC"


def _iso(instant: datetime) -> str:
    """L'heure d'arrivée telle qu'on la redonne pour reprendre : exacte, microsecondes comprises s'il y en a."""
    forme = "%Y-%m-%dT%H:%M:%S.%fZ" if instant.microsecond else "%Y-%m-%dT%H:%M:%SZ"
    return instant.strftime(forme)


def _conclure_le_resultat(passage: ResultatLivraison, sortie: Callable[[str], Any]) -> None:
    registre = passage.ligne_registre
    codes = list(dict.fromkeys(alerte.code for alerte in passage.alertes))
    sortie(f"Statut : {registre.statut}")
    sortie(f"Lignes : {registre.nb_recues} reçues, {registre.nb_valides} valides, {registre.nb_rejetees} rejetées")
    sortie(f"Alertes : {', '.join(codes) if codes else 'aucune'}")
    sortie(
        f"Couches : bronze {len(passage.lignes_bronze)}, coupées {len(passage.lignes_coupees)}, "
        f"silver {len(passage.silver.lignes)}, quarantaine {len(passage.silver.quarantaine)}"
    )


def _lecture_de_la_simulation(
    contrat: Contrat, catalogue: str, executeur: Callable[..., Any], magasin: MagasinSql, sortie: Callable[[str], Any]
) -> Callable[[], tuple[list[LigneRegistre], list[LigneSilver]]]:
    """La lecture de l'état en simulation : les tables ne sont pas créées, donc on regarde d'abord lesquelles existent ; s'il en manque,
    l'état est supposé vide (et on le dit), sinon on le lit comme `passer` le ferait."""

    def lire() -> tuple[list[LigneRegistre], list[LigneSilver]]:
        presence = verifier_la_presence(contrat, catalogue, executeur)
        absentes = sorted(nom for nom, presente in presence.tables.items() if not presente)
        if absentes:
            sortie(f"Tables absentes ou invisibles pour ce profil : {', '.join(absentes)} : "
                   f"l'état est supposé vide, la décision peut différer.")
            return [], []
        return lire_l_etat(contrat, magasin)

    return lire


def _echec(phase: str, erreur: Exception, sortie: Callable[[str], Any], reprise: str) -> int:
    """Ce que le programme dit quand `passer` échoue, selon la dernière étape annoncée : toujours le code 1."""
    cause = borne(f"{type(erreur).__name__} : {erreur}")
    if phase == "lecture":
        sortie(f"Lecture de l'état impossible : {cause}")
        sortie("Si les tables n'existent pas encore, --ecrire les crée.")
    elif phase == "traitement":
        sortie(f"Traitement de la livraison impossible : {cause}")
    elif isinstance(erreur, DecisionManquante):                      # refusée AVANT d'écrire quoi que ce soit : reprendre l'autre d'abord
        sortie(f"Écriture refusée, rien n'a été écrit : {erreur}")
        sortie("Reprendre d'abord la livraison citée, avec son heure d'arrivée, puis relancer celle-ci.")
    else:
        sortie(f"Écriture impossible : {cause}")
        sortie(f"Des couches ont pu être écrites, pas le registre. Pour reprendre : {reprise}.")
    return 1


def principal(
    argv: Sequence[str] | None = None,
    *,
    fabriquer: Callable[..., Callable[..., Any]] = fabriquer_executeur_reel,
    sortie: Callable[[str], Any] = print,
    horloge: Callable[[], float] = time.monotonic,
    maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    arguments = _lecteur_d_arguments().parse_args(argv)
    try:
        return _passer(arguments, fabriquer, sortie, horloge, maintenant)
    except KeyboardInterrupt:
        sortie("Interrompu. Rien n'a été créé ni écrit.")
        return 130


def _passer(
    arguments: argparse.Namespace, fabriquer: Callable[..., Callable[..., Any]], sortie: Callable[[str], Any],
    horloge: Callable[[], float], maintenant: Callable[[], datetime],
) -> int:
    # 1. Tout ce qui se refuse SANS se connecter : catalogue, fichier, contrat, catalogue d'alertes, heure.
    if arguments.catalogue != CATALOGUE_PAR_DEFAUT:
        sortie(f"Le passage ne s'exécute que sur le catalogue playground ({CATALOGUE_PAR_DEFAUT}), pas sur « {arguments.catalogue} ».")
        return 2
    chemin = Path(arguments.fichier)
    if not chemin.is_file():
        sortie(f"Fichier introuvable : {borne(str(chemin))}. Donner le chemin d'une livraison avec --fichier.")
        return 2
    try:
        contrat = charger_contrat(Path(arguments.contrat))
    except (OSError, ContratInvalide) as erreur:
        sortie(f"Contrat illisible : {borne(str(erreur))}. Lancer depuis la racine du dépôt, ou donner le chemin avec --contrat.")
        return 2
    try:
        alertes = charger_catalogue(Path(arguments.alertes))
    except (OSError, CatalogueInvalide) as erreur:
        sortie(f"Catalogue d'alertes illisible : {borne(str(erreur))}. Lancer depuis la racine du dépôt, ou donner --alertes.")
        return 2
    try:
        heure_arrivee = _lire_l_heure(arguments.heure_arrivee, maintenant)
    except ValueError as erreur:
        sortie(f"--heure-arrivee : {erreur}")
        return 2

    sortie(f"Passage de {chemin.name} sur le catalogue {arguments.catalogue}.")
    sortie(f"Arrivée : {_affichee(heure_arrivee)}")
    sortie(f"Connexion à l'entrepôt… (délai de chaque requête : {arguments.delai:g} s)")
    reprise = f"relancer avec --ecrire --heure-arrivee {_iso(heure_arrivee)}, sans rien dupliquer"

    # 2. La connexion, puis les phases : creation (avec --ecrire), lecture, traitement, ecriture.
    phase = "connexion"
    debut = horloge()
    try:
        try:
            executeur = fabriquer(arguments.profil, arguments.entrepot, attente_max=arguments.delai)
            executeur(SQL_CONNEXION, {})
        except Exception as erreur:
            sortie(f"Connexion impossible après {duree(horloge() - debut)} : {borne(f'{type(erreur).__name__} : {erreur}')}")
            if not isinstance(erreur, SdkAbsent):
                sortie(CAUSES_A_VERIFIER)
            return 2
        sortie(f"Connexion établie ({duree(horloge() - debut)})")
        magasin = MagasinSql(executeur, arguments.catalogue)

        if arguments.ecrire:
            phase = "creation"
            try:
                creer_tables(contrat, arguments.catalogue, executeur)
            except Exception as erreur:
                sortie(f"Création des tables impossible : {borne(f'{type(erreur).__name__} : {erreur}')}")
                return 1
        lire = None if arguments.ecrire else _lecture_de_la_simulation(contrat, arguments.catalogue, executeur, magasin, sortie)

        def jalon(nom: str, rendu: ResultatLivraison | None) -> None:
            nonlocal phase
            if nom == "traite":
                phase = "conclusion"                        # une sortie qui échoue ici n'est pas une panne du traitement : elle s'échappe
                if rendu is None:                           # `passer` le donne toujours ; un `assert` disparaîtrait avec `python -O`
                    raise RuntimeError("passer a annoncé « traite » sans résultat")
                _conclure_le_resultat(rendu, sortie)
            else:
                phase = nom

        try:
            passer(contrat, alertes, chemin, magasin, heure_arrivee, ecrire=arguments.ecrire, lire=lire, jalon=jalon)
        except Exception as erreur:
            if phase == "conclusion":
                raise
            return _echec(phase, erreur, sortie, reprise)
        if not arguments.ecrire:
            sortie("Simulation : rien n'a été créé ni écrit. Relancer avec --ecrire pour écrire dans les tables.")
            return 0
        sortie(f"Écrit dans {arguments.catalogue} : les couches de données d'abord, le registre en dernier.")
        return 0
    except KeyboardInterrupt:
        if phase == "ecriture":
            sortie(f"Interrompu pendant l'écriture. Des couches ont pu être écrites, pas le registre. Pour reprendre : {reprise}.")
        elif not arguments.ecrire or phase == "connexion":
            sortie("Interrompu. Rien n'a été créé ni écrit" + (" : simulation." if not arguments.ecrire else "."))
        elif phase == "creation":
            sortie("Interrompu pendant la création des tables : des tables ont pu être créées, aucune de ses lignes n'a été écrite.")
        else:
            sortie("Interrompu avant l'écriture : les tables existent, aucune ligne de la livraison n'a été écrite.")
        return 130

if __name__ == "__main__":
    raise SystemExit(principal())
