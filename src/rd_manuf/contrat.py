"""Chargement d'un contrat de données (YAML) et contrôle d'une ligne contre ce contrat.

Cycle A : `charger_contrat` (auto-contrôle du contrat) et `controler_ligne` (types, obligatoire, plages,
motif, règles entre colonnes). Le contrôle d'une livraison entière arrive au cycle B.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

# ==============================================================================
# 1. 📚 VOCABULAIRE DU CONTRAT
# ==============================================================================
TYPE_CHAINE = "chaine"
TYPE_ENTIER = "entier"
TYPE_DECIMAL = "decimal"
TYPE_DATE = "date"
TYPES_SUPPORTES = (TYPE_CHAINE, TYPE_ENTIER, TYPE_DECIMAL, TYPE_DATE)

REGLE_INFERIEUR_OU_EGAL = "inferieur_ou_egal"
REGLES_INTER_SUPPORTEES = (REGLE_INFERIEUR_OU_EGAL,)

# Textes qui, dans un export de capteur ou de CSV, signifient « pas de valeur » (comparés en minuscules, sans espaces).
# La chaîne vide en fait partie : « " " » et « "" » se réduisent à "" après strip.
MARQUEURS_ABSENCE = frozenset({"", "nan", "nans", "null", "na"})

CLES_OBLIGATOIRES_CONTRAT = ("nom", "version", "cle_primaire", "colonnes", "seuils_qualite")
CLES_OBLIGATOIRES_REGLE = ("nom", "type", "gauche", "droite")

# Une ligne malformée n'a pas de colonnes exploitables : elle est conservée telle que lue sous ces deux clés (diagnostic, quarantaine).
CLE_NUMERO_LIGNE = "numero_ligne"
CLE_CHAMPS_BRUTS = "champs_bruts"


class ContratInvalide(ValueError):
    """Le fichier YAML du contrat est mal formé (erreur de conception, pas de données)."""


# ==============================================================================
# 2. 🧱 STRUCTURES
# ==============================================================================
@dataclass(frozen=True)
class Colonne:
    nom: str
    type: str
    obligatoire: bool = True
    minimum: float | None = None
    maximum: float | None = None
    motif: str | None = None
    valeurs_autorisees: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RegleInter:
    nom: str
    type: str
    gauche: str
    droite: str


@dataclass(frozen=True)
class Contrat:
    nom: str
    version: str
    cle_primaire: tuple[str, ...]
    colonnes: tuple[Colonne, ...]
    regles_inter_colonnes: tuple[RegleInter, ...]
    taux_rejet_max: float
    brut: dict[str, Any] = field(repr=False, default_factory=dict)  # YAML d'origine


class StatutLivraison(str, Enum):
    """Décision finale sur une livraison."""

    ACCEPTEE = "ACCEPTEE"                            # aucune ligne rejetée
    ACCEPTEE_AVEC_REJETS = "ACCEPTEE_AVEC_REJETS"    # rejets dans le seuil : valides intégrées, rejets en quarantaine
    BLOQUEE = "BLOQUEE"                              # au-dessus du seuil : RIEN n'est intégré (tout ou rien)
    REFUSEE = "REFUSEE"                              # structure invalide : aucune ligne lue (jamais produit par `valider_livraison`)


class CodeErreur(str, Enum):
    """Code STABLE d'une violation : les alertes et les tableaux de bord s'appuient dessus, jamais sur le texte du message.

    Renommer un code est un changement de contrat avec les consommateurs (voir contracts/alertes/catalogue.yaml).
    """

    OBLIGATOIRE_MANQUANT = "OBLIGATOIRE_MANQUANT"
    TYPE_INVALIDE = "TYPE_INVALIDE"
    HORS_PLAGE = "HORS_PLAGE"
    MOTIF_NON_RESPECTE = "MOTIF_NON_RESPECTE"
    VALEUR_HORS_DOMAINE = "VALEUR_HORS_DOMAINE"
    REGLE_ENTRE_COLONNES = "REGLE_ENTRE_COLONNES"
    CLE_EN_CONFLIT = "CLE_EN_CONFLIT"
    LIGNE_MALFORMEE = "LIGNE_MALFORMEE"    # nombre de champs différent de l'en-tête (ligne coupée, virgule non protégée)


@dataclass(frozen=True)
class Violation:
    code: CodeErreur
    colonnes: tuple[str, ...]   # colonnes à corriger
    message: str                # texte pour un humain


@dataclass(frozen=True)
class LigneMalformee:
    """Une ligne dont le nombre de champs n'est pas celui de l'en-tête : ses valeurs ne peuvent pas être associées aux colonnes."""

    numero: int                        # numéro dans le fichier, l'en-tête étant la ligne 1 (comme dans Excel)
    champs: tuple[str, ...]            # valeurs brutes lues, pour le diagnostic et la quarantaine


@dataclass(frozen=True)
class LigneRejetee:
    ligne: dict[str, Any]
    violations: tuple[Violation, ...]

    @property
    def erreurs(self) -> tuple[str, ...]:
        return tuple(violation.message for violation in self.violations)


@dataclass(frozen=True)
class ResultatValidation:
    statut: StatutLivraison
    lignes_valides: list[dict[str, Any]]      # vide si la livraison est BLOQUEE
    lignes_rejetees: list[LigneRejetee]       # toujours renseignées, même si BLOQUEE (diagnostic)
    nb_recues: int
    nb_doublons_exacts: int                   # rejeux strictement identiques : ignorés, ni rejetés ni comptés comme rejets
    taux_rejet: float                         # lignes_rejetees / nb_recues
    version_contrat: str                      # version du contrat avec laquelle la livraison a été jugée


# ==============================================================================
# 3. 📥 CHARGEMENT ET AUTO-CONTRÔLE DU CONTRAT
# ==============================================================================
def _lire_yaml(chemin: str | Path) -> dict[str, Any]:
    """Lit le fichier ; tout problème de lecture/syntaxe devient une ContratInvalide claire."""
    try:
        brut = yaml.safe_load(Path(chemin).read_text(encoding="utf-8"))
    except yaml.YAMLError as erreur:
        raise ContratInvalide(f"{chemin} : YAML illisible ({erreur}).") from erreur
    if not isinstance(brut, dict):
        raise ContratInvalide(f"{chemin} : le contrat doit être un dictionnaire YAML.")
    manquantes = [cle for cle in CLES_OBLIGATOIRES_CONTRAT if cle not in brut]
    if manquantes:
        raise ContratInvalide(f"{chemin} : clés manquantes {manquantes}.")
    return brut


def _construire_colonne(definition: Any) -> Colonne:
    if not isinstance(definition, dict):
        raise ContratInvalide(f"Une colonne doit être un dictionnaire (nom, type, ...), reçu : {definition!r}.")
    nom = definition.get("nom")
    if not isinstance(nom, str) or not nom.strip():
        raise ContratInvalide(f"Colonne sans nom valide : {definition!r}.")
    if definition.get("type") not in TYPES_SUPPORTES:
        raise ContratInvalide(f"Colonne « {nom} » : type inconnu « {definition.get('type')} ».")

    minimum, maximum = definition.get("minimum"), definition.get("maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ContratInvalide(f"Colonne « {nom} » : minimum {minimum} supérieur au maximum {maximum}.")

    motif = definition.get("motif")
    if motif is not None:
        try:
            re.compile(motif)
        except re.error as erreur:
            raise ContratInvalide(f"Colonne « {nom} » : motif invalide « {motif} » ({erreur}).") from erreur

    return Colonne(
        nom=nom,
        type=definition["type"],
        obligatoire=definition.get("obligatoire", True),
        minimum=minimum,
        maximum=maximum,
        motif=motif,
        valeurs_autorisees=tuple(definition["valeurs_autorisees"]) if "valeurs_autorisees" in definition else None,
    )


def _construire_regle(definition: Any, noms_colonnes: set[str]) -> RegleInter:
    if not isinstance(definition, dict):
        raise ContratInvalide(f"Une règle doit être un dictionnaire, reçu : {definition!r}.")
    manquantes = [cle for cle in CLES_OBLIGATOIRES_REGLE if cle not in definition]
    if manquantes:
        raise ContratInvalide(f"Règle « {definition.get('nom', '?')} » : clés manquantes {manquantes}.")
    if definition["type"] not in REGLES_INTER_SUPPORTEES:
        raise ContratInvalide(f"Règle « {definition['nom']} » : type inconnu « {definition['type']} ».")
    for cle in ("gauche", "droite"):
        if definition[cle] not in noms_colonnes:
            raise ContratInvalide(f"Règle « {definition['nom']} » : colonne « {definition[cle]} » inconnue.")
    return RegleInter(definition["nom"], definition["type"], definition["gauche"], definition["droite"])


def _lire_taux_rejet_max(seuils: Any) -> float:
    if not isinstance(seuils, dict) or "taux_rejet_max" not in seuils:
        raise ContratInvalide("seuils_qualite doit être un dictionnaire contenant « taux_rejet_max ».")
    taux = seuils["taux_rejet_max"]
    if isinstance(taux, bool) or not isinstance(taux, (int, float)):
        raise ContratInvalide(f"taux_rejet_max doit être un nombre (reçu {taux!r}).")
    if not 0 <= taux <= 1:
        raise ContratInvalide(f"taux_rejet_max doit être entre 0 et 1 (reçu {taux}).")
    return float(taux)


def charger_contrat(chemin: str | Path) -> Contrat:
    """Lit le YAML et vérifie qu'il est cohérent AVANT de s'en servir (toute erreur = ContratInvalide)."""
    brut = _lire_yaml(chemin)

    if not isinstance(brut["colonnes"], list):
        raise ContratInvalide("colonnes doit être une liste.")
    colonnes = [_construire_colonne(definition) for definition in brut["colonnes"]]
    noms = [colonne.nom for colonne in colonnes]
    doublons = sorted({nom for nom in noms if noms.count(nom) > 1})
    if doublons:
        raise ContratInvalide(f"Colonnes déclarées plusieurs fois : {doublons}.")

    if not isinstance(brut["cle_primaire"], list) or not brut["cle_primaire"]:
        raise ContratInvalide("cle_primaire doit être une liste non vide de noms de colonnes (sans clé, aucun doublon n'est détecté).")
    par_nom = {colonne.nom: colonne for colonne in colonnes}
    for cle in brut["cle_primaire"]:
        if cle not in par_nom:
            raise ContratInvalide(f"Clé primaire « {cle} » absente des colonnes.")
        if not par_nom[cle].obligatoire:
            raise ContratInvalide(f"Clé primaire « {cle} » : la colonne doit être obligatoire (une clé absente n'identifie rien).")

    regles = [_construire_regle(definition, set(noms)) for definition in brut.get("regles_inter_colonnes") or []]

    return Contrat(
        nom=brut["nom"],
        version=str(brut["version"]),
        cle_primaire=tuple(brut["cle_primaire"]),
        colonnes=tuple(colonnes),
        regles_inter_colonnes=tuple(regles),
        taux_rejet_max=_lire_taux_rejet_max(brut["seuils_qualite"]),
        brut=brut,
    )


# ==============================================================================
# 4. 🔎 CONTRÔLE D'UNE LIGNE
# ==============================================================================
def est_absente(valeur: Any) -> bool:
    """Une valeur est absente si elle est None, un NaN réel, une chaîne vide/blanche ou un marqueur d'absence.

    La comparaison aux marqueurs est EXACTE (après strip et minuscules) : « nano » ou « 0 » restent des valeurs.
    """
    if valeur is None:
        return True
    if isinstance(valeur, float):
        return math.isnan(valeur)
    return isinstance(valeur, str) and valeur.strip().lower() in MARQUEURS_ABSENCE


def convertir_valeur(type_colonne: str, valeur: Any) -> Any:
    """Convertit vers le type du contrat. Lève ValueError si impossible."""
    if type_colonne == TYPE_CHAINE:
        return str(valeur).strip()
    if type_colonne == TYPE_ENTIER:
        if isinstance(valeur, bool) or (isinstance(valeur, float) and not valeur.is_integer()):
            raise ValueError("entier attendu")
        return int(valeur)
    if type_colonne == TYPE_DECIMAL:
        if isinstance(valeur, bool):
            raise ValueError("décimal attendu")
        nombre = float(valeur)
        if not math.isfinite(nombre):   # inf / -inf : de vrais nombres aberrants, jamais une absence
            raise ValueError("décimal fini attendu")
        return nombre
    if type_colonne == TYPE_DATE:
        return valeur if isinstance(valeur, date) else date.fromisoformat(str(valeur).strip())
    raise ValueError(f"type inconnu {type_colonne}")


def diagnostiquer_ligne(contrat: Contrat, ligne: dict[str, Any]) -> list[Violation]:
    """Retourne les violations de la ligne, chacune avec son code stable et ses colonnes (liste vide = ligne conforme)."""
    violations: list[Violation] = []
    valeurs: dict[str, Any] = {}

    for colonne in contrat.colonnes:
        nom = colonne.nom
        brute = ligne.get(nom)
        if est_absente(brute):
            if colonne.obligatoire:
                violations.append(Violation(CodeErreur.OBLIGATOIRE_MANQUANT, (nom,), f"{nom} : valeur obligatoire manquante"))
            continue
        try:
            valeur = convertir_valeur(colonne.type, brute)
        except (ValueError, TypeError):
            violations.append(Violation(CodeErreur.TYPE_INVALIDE, (nom,), f"{nom} : type {colonne.type} invalide (« {brute} »)"))
            continue
        valeurs[nom] = valeur

        if colonne.minimum is not None and valeur < colonne.minimum:
            violations.append(Violation(CodeErreur.HORS_PLAGE, (nom,), f"{nom} : {valeur} < minimum {colonne.minimum}"))
        if colonne.maximum is not None and valeur > colonne.maximum:
            violations.append(Violation(CodeErreur.HORS_PLAGE, (nom,), f"{nom} : {valeur} > maximum {colonne.maximum}"))
        if colonne.motif and not re.search(colonne.motif, str(valeur)):
            message = f"{nom} : « {valeur} » ne respecte pas le motif {colonne.motif}"
            violations.append(Violation(CodeErreur.MOTIF_NON_RESPECTE, (nom,), message))
        if colonne.valeurs_autorisees and str(valeur) not in colonne.valeurs_autorisees:
            message = f"{nom} : « {valeur} » hors de {list(colonne.valeurs_autorisees)}"
            violations.append(Violation(CodeErreur.VALEUR_HORS_DOMAINE, (nom,), message))

    for regle in contrat.regles_inter_colonnes:
        if regle.gauche in valeurs and regle.droite in valeurs and not valeurs[regle.gauche] <= valeurs[regle.droite]:
            message = f"règle {regle.nom} : {regle.gauche}={valeurs[regle.gauche]} > {regle.droite}={valeurs[regle.droite]}"
            violations.append(Violation(CodeErreur.REGLE_ENTRE_COLONNES, (regle.gauche, regle.droite), message))

    return violations


def controler_ligne(contrat: Contrat, ligne: dict[str, Any]) -> list[str]:
    """Même contrôle que `diagnostiquer_ligne`, réduit aux messages lisibles (liste vide = ligne conforme)."""
    return [violation.message for violation in diagnostiquer_ligne(contrat, ligne)]


# ==============================================================================
# 5. 📦 VALIDATION D'UNE LIVRAISON
# ==============================================================================
def _valeurs_normalisees(contrat: Contrat, ligne: dict[str, Any]) -> dict[str, Any]:
    """Valeurs de la ligne telles que le CONTRAT les comprend : toute forme d'absence devient None, le reste est converti.

    Sert à juger si deux lignes sont « identiques » : 1000 et "1000", " L1 " et "L1", ou deux NaN créés séparément
    (nan != nan en Python) sont la même information. À n'appeler que sur une ligne déjà validée par `diagnostiquer_ligne`.
    Les colonnes absentes du contrat ne comptent pas dans la comparaison.
    """
    valeurs: dict[str, Any] = {}
    for colonne in contrat.colonnes:
        brute = ligne.get(colonne.nom)
        valeurs[colonne.nom] = None if est_absente(brute) else convertir_valeur(colonne.type, brute)
    return valeurs


def _rejeter_ligne_malformee(contrat: Contrat, malformee: LigneMalformee) -> LigneRejetee:
    message = f"ligne {malformee.numero} : {len(malformee.champs)} champs reçus au lieu de {len(contrat.colonnes)}"
    violation = Violation(CodeErreur.LIGNE_MALFORMEE, (), message)     # aucune colonne précise à corriger : c'est la ligne entière
    ligne = {CLE_NUMERO_LIGNE: malformee.numero, CLE_CHAMPS_BRUTS: list(malformee.champs)}
    return LigneRejetee(ligne=ligne, violations=(violation,))


def valider_livraison(
    contrat: Contrat,
    lignes: list[dict[str, Any]],
    lignes_malformees: Sequence[LigneMalformee] = (),
) -> ResultatValidation:
    """Applique le contrat à une livraison : lignes valides / rejetées, doublons, puis décision selon le seuil.

    Pour une même clé primaire, la PREMIÈRE ligne valide arrivée fait foi :
        - une ligne identique APRÈS normalisation par le contrat est un rejeu technique : ignorée, sans rejet ;
        - une ligne au contenu différent est un conflit : rejetée (on ne sait pas laquelle est juste).
    Une ligne invalide ne réserve pas sa clé.
    Les lignes malformées (mises à part par le lecteur) sont rejetées avec LIGNE_MALFORMEE : elles comptent comme reçues et comme rejetées.
    """
    valides: list[dict[str, Any]] = []
    rejetees: list[LigneRejetee] = []
    premiere_par_cle: dict[tuple[str, ...], dict[str, Any]] = {}
    nb_doublons_exacts = 0

    for ligne in lignes:
        violations = diagnostiquer_ligne(contrat, ligne)
        if violations:
            rejetees.append(LigneRejetee(ligne=ligne, violations=tuple(violations)))
            continue

        valeurs = _valeurs_normalisees(contrat, ligne)
        cle = tuple(str(valeurs[nom]) for nom in contrat.cle_primaire)
        deja_vue = premiere_par_cle.get(cle)
        if deja_vue is None:
            premiere_par_cle[cle] = valeurs
            valides.append(ligne)
        elif deja_vue == valeurs:
            nb_doublons_exacts += 1
        else:
            message = f"clé primaire {list(contrat.cle_primaire)} en conflit : {', '.join(cle)} (même clé, contenu différent)"
            conflit = Violation(CodeErreur.CLE_EN_CONFLIT, contrat.cle_primaire, message)
            rejetees.append(LigneRejetee(ligne=ligne, violations=(conflit,)))

    rejetees.extend(_rejeter_ligne_malformee(contrat, malformee) for malformee in sorted(lignes_malformees, key=lambda ligne: ligne.numero))
    nb_recues = len(lignes) + len(lignes_malformees)
    taux_rejet = len(rejetees) / nb_recues if nb_recues else 0.0

    # Comparaison par DIVISION, jamais `rejets > seuil * total` : 0.29 * 100 vaut 28.999999999999996 en flottant.
    if taux_rejet > contrat.taux_rejet_max:
        statut = StatutLivraison.BLOQUEE
        valides = []
    elif rejetees:
        statut = StatutLivraison.ACCEPTEE_AVEC_REJETS
    else:
        statut = StatutLivraison.ACCEPTEE

    return ResultatValidation(
        statut=statut,
        lignes_valides=valides,
        lignes_rejetees=rejetees,
        nb_recues=nb_recues,
        nb_doublons_exacts=nb_doublons_exacts,
        taux_rejet=taux_rejet,
        version_contrat=contrat.version,
    )
