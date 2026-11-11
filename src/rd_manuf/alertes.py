"""Alertes : à partir du résultat de validation d'une livraison, décide QUI doit agir et QUI doit être informé.

Ce module ne contient aucun envoi et aucune personne : il produit des `Alerte` portant des RÔLES.
Les règles viennent de contracts/alertes/catalogue.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rd_manuf.contrat import CodeErreur, ResultatValidation, StatutLivraison
from rd_manuf.reception import Reception
from rd_manuf.structure import CodeStructure

CODE_LIVRAISON_BLOQUEE = "LIVRAISON_BLOQUEE"
CLES_OBLIGATOIRES_CATALOGUE = ("roles", "toujours_informes", "codes", "structure", "livraison_bloquee")


class CatalogueInvalide(ValueError):
    """Le catalogue d'alertes est illisible ou incohérent : à corriger avant tout envoi."""


@dataclass(frozen=True)
class RegleRoutage:
    agit: str
    informe: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalogue:
    roles: tuple[str, ...]
    toujours_informes: tuple[str, ...]
    regles: dict[str, RegleRoutage]
    regles_structure: dict[str, RegleRoutage]
    livraison_bloquee: RegleRoutage

    def regle(self, code: str) -> RegleRoutage:
        return self.regles[CodeErreur(code).value]

    def regle_structure(self, code: str) -> RegleRoutage:
        return self.regles_structure[CodeStructure(code).value]


@dataclass(frozen=True)
class Alerte:
    livraison_id: str
    code: str
    nb_lignes: int
    agit: str
    informes: tuple[str, ...]
    colonnes: tuple[str, ...]


# ==============================================================================
# 📥 CHARGEMENT ET AUTO-CONTRÔLE DU CATALOGUE
# ==============================================================================
def _est_role_declare(valeur: Any, roles: set[str]) -> bool:
    """Vérifie le type AVANT la recherche : chercher une liste ou un dictionnaire dans un ensemble lève un TypeError."""
    return isinstance(valeur, str) and valeur in roles


def _liste_de_roles(valeur: Any, roles: set[str], description: str) -> tuple[str, ...]:
    if not isinstance(valeur, list):
        raise CatalogueInvalide(f"{description} doit être une liste de rôles (reçu {valeur!r}).")
    inconnus = [role for role in valeur if not _est_role_declare(role, roles)]
    if inconnus:
        raise CatalogueInvalide(f"{description} : rôles inconnus {inconnus} (rôles déclarés : {sorted(roles)}).")
    return tuple(valeur)


def _construire_regle(definition: Any, roles: set[str], description: str) -> RegleRoutage:
    if not isinstance(definition, dict) or "agit" not in definition:
        raise CatalogueInvalide(f"{description} : il faut un dictionnaire avec la clé « agit ».")
    if not _est_role_declare(definition["agit"], roles):
        raise CatalogueInvalide(f"{description} : « agit » = {definition['agit']!r} n'est pas un rôle déclaré.")
    informe = _liste_de_roles(definition.get("informe", []), roles, f"{description} : informe")
    return RegleRoutage(agit=definition["agit"], informe=informe)


def _construire_regles(section: Any, attendus: set[str], roles: set[str], nom_section: str) -> dict[str, RegleRoutage]:
    """Une règle par code attendu, ni plus ni moins : un code oublié ou inventé est refusé au chargement."""
    if not isinstance(section, dict):
        raise CatalogueInvalide(f"{nom_section} doit être un dictionnaire {{code: règle}}.")
    sans_regle = sorted(attendus - set(section))
    if sans_regle:
        raise CatalogueInvalide(f"{nom_section} : codes sans règle de routage : {sans_regle}.")
    inconnus = sorted(set(section) - attendus)
    if inconnus:
        raise CatalogueInvalide(f"{nom_section} : règles pour des codes qui n'existent pas : {inconnus}.")
    return {code: _construire_regle(section[code], roles, f"{nom_section} {code}") for code in sorted(attendus)}


def charger_catalogue(chemin: str | Path) -> Catalogue:
    """Lit le YAML et vérifie qu'il est cohérent AVANT de s'en servir (toute erreur = CatalogueInvalide)."""
    try:
        brut = yaml.safe_load(Path(chemin).read_text(encoding="utf-8"))
    except yaml.YAMLError as erreur:
        raise CatalogueInvalide(f"{chemin} : YAML illisible ({erreur}).") from erreur
    if not isinstance(brut, dict):
        raise CatalogueInvalide(f"{chemin} : le catalogue doit être un dictionnaire YAML.")
    manquantes = [cle for cle in CLES_OBLIGATOIRES_CATALOGUE if cle not in brut]
    if manquantes:
        raise CatalogueInvalide(f"{chemin} : clés manquantes {manquantes}.")

    if not isinstance(brut["roles"], list) or not all(isinstance(role, str) and role for role in brut["roles"]):
        raise CatalogueInvalide("roles doit être une liste de noms de rôles.")
    roles = set(brut["roles"])
    toujours_informes = _liste_de_roles(brut["toujours_informes"], roles, "toujours_informes")

    regles = _construire_regles(brut["codes"], {code.value for code in CodeErreur}, roles, "codes")
    regles_structure = _construire_regles(brut["structure"], {code.value for code in CodeStructure}, roles, "structure")

    return Catalogue(
        roles=tuple(brut["roles"]),
        toujours_informes=toujours_informes,
        regles=regles,
        regles_structure=regles_structure,
        livraison_bloquee=_construire_regle(brut["livraison_bloquee"], roles, "livraison_bloquee"),
    )


# ==============================================================================
# 🚦 ROUTAGE
# ==============================================================================
def _informes(catalogue: Catalogue, regle: RegleRoutage) -> tuple[str, ...]:
    """Informés = toujours informés + informés du code, sans doublon, sans celui qui agit, triés."""
    return tuple(sorted((set(catalogue.toujours_informes) | set(regle.informe)) - {regle.agit}))


def router_alertes(catalogue: Catalogue, resultat: ResultatValidation, livraison_id: str) -> list[Alerte]:
    """Une alerte par code d'erreur (jamais une par ligne), triées par code ; `LIVRAISON_BLOQUEE` d'abord si elle l'est.

    Un rejeu technique (doublon exact) n'est pas un rejet : il ne produit aucune alerte.
    """
    lignes_par_code: dict[str, int] = {}
    colonnes_par_code: dict[str, set[str]] = {}
    for rejet in resultat.lignes_rejetees:
        for code in {violation.code for violation in rejet.violations}:
            lignes_par_code[code.value] = lignes_par_code.get(code.value, 0) + 1
        for violation in rejet.violations:
            colonnes_par_code.setdefault(violation.code.value, set()).update(violation.colonnes)

    alertes = []
    if resultat.statut == StatutLivraison.BLOQUEE:
        regle = catalogue.livraison_bloquee
        alertes.append(
            Alerte(livraison_id, CODE_LIVRAISON_BLOQUEE, len(resultat.lignes_rejetees), regle.agit, _informes(catalogue, regle), ())
        )
    for code in sorted(lignes_par_code):
        regle = catalogue.regle(code)
        colonnes = tuple(sorted(colonnes_par_code[code]))
        alertes.append(Alerte(livraison_id, code, lignes_par_code[code], regle.agit, _informes(catalogue, regle), colonnes))
    return alertes


def router_alertes_reception(catalogue: Catalogue, reception: Reception) -> list[Alerte]:
    """Point d'entrée unique après une réception.

    Livraison REFUSEE : une alerte par code de structure, triées par code. `nb_lignes` = lignes reçues et NON intégrées à cause du
    refus (aucune n'a été jugée) ; un fichier vide donne 0. Sinon : le routage des lignes, inchangé.
    """
    if reception.statut != StatutLivraison.REFUSEE:
        return router_alertes(catalogue, reception.resultat, reception.livraison_id)

    alertes = []
    for probleme in sorted(reception.problemes_structure, key=lambda probleme: probleme.code.value):
        regle = catalogue.regle_structure(probleme.code)
        informes = _informes(catalogue, regle)
        alertes.append(Alerte(reception.livraison_id, probleme.code.value, reception.nb_recues, regle.agit, informes, probleme.colonnes))
    return alertes
