"""Fixtures partagées : on teste le VRAI contrat du dépôt, pas une copie.

Principe d'organisation : toute valeur qui sert à FABRIQUER une ligne (valide, hors plage, conflictuelle...) est définie ICI,
une seule fois, et dérivée du contrat. Un test dont le sujet n'est pas le contrat ne contient donc aucun chiffre du contrat
(401, 100001...) : si le contrat change, il suit sans qu'on ait à le rééditer.
Les tests qui VÉRIFIENT le contrat gardent leurs valeurs en clair, regroupées dans une table `parametrize` en tête de test.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from rd_manuf.contrat import charger_contrat

RACINE_DEPOT = Path(__file__).resolve().parents[2]
CHEMIN_CONTRAT = RACINE_DEPOT / "contracts" / "data" / "livraison_production.yaml"

SEUIL_NEUTRALISE = 1.0   # taux de rejet max à 100 % : le statut de la livraison ne dépend plus du seuil
SEUIL_STRICT = 0.0       # taux de rejet max à 0 % : une seule ligne rejetée suffit à bloquer, quel que soit le volume
VALEUR_HORS_DOMAINE = "INCONNUE"   # absente de toute liste `valeurs_autorisees` raisonnable
COLONNE_DECIMALE_BORNEE = "temperature_c"


@pytest.fixture(scope="session")
def contrat():
    return charger_contrat(CHEMIN_CONTRAT)


@pytest.fixture(scope="session")
def contrat_sans_seuil(contrat):
    """Même contrat, seuil de rejet neutralisé : sert à tester autre chose que le blocage (agrégation, codes, routage).

    Piège : « neutralisé » veut dire seuil à 100 %, et un taux de 100 % ne passe sous ce seuil que si la comparaison est stricte (`>`).
    Une livraison dont TOUTES les lignes sont rejetées dépend donc de ce détail du contrat. Règle : garder au moins une ligne valide
    dans toute livraison construite avec cette fixture (voir `test_alertes.py`).
    """
    return replace(contrat, taux_rejet_max=SEUIL_NEUTRALISE)


@pytest.fixture(scope="session")
def contrat_strict(contrat):
    """Même contrat, seuil à zéro : toute livraison avec au moins un rejet est BLOQUÉE (sans calcul de pourcentage)."""
    return replace(contrat, taux_rejet_max=SEUIL_STRICT)


@pytest.fixture()
def ligne_conforme():
    """Une ligne qui respecte le contrat : chaque test en modifie UN champ pour viser UNE règle."""
    return {
        "id_lot": "LOT-000001",
        "id_ligne": "L1",
        "date_production": "2026-11-01",
        "quantite_produite": 1000,
        "quantite_rebut": 10,
        "temperature_c": 200.0,
    }


class FabriqueDeLignes:
    """Fabrique des lignes dont la validité est connue d'avance, quelles que soient les bornes du contrat."""

    def __init__(self, contrat, modele):
        self._contrat = contrat
        self._modele = modele

    def _borne_haute(self, colonne):
        return next(c for c in self._contrat.colonnes if c.nom == colonne).maximum

    def _avec_cle(self, numero, **modifications):
        return dict(self._modele, id_lot=f"LOT-{numero:06d}", **modifications)

    def valide(self, numero=1):
        return self._avec_cle(numero)

    def valides(self, nombre, premier=1):
        return [self.valide(numero) for numero in range(premier, premier + nombre)]

    def hors_plage(self, numero):
        """Violation HORS_PLAGE : température juste au-dessus du maximum du contrat."""
        return self._avec_cle(numero, **{COLONNE_DECIMALE_BORNEE: self._borne_haute(COLONNE_DECIMALE_BORNEE) + 1})

    def livraison(self, nb_valides, nb_hors_plage=0):
        """Une livraison de lignes toutes distinctes : `nb_valides` conformes, puis `nb_hors_plage` en HORS_PLAGE."""
        hors_plage = [self.hors_plage(numero) for numero in range(nb_valides + 1, nb_valides + nb_hors_plage + 1)]
        return self.valides(nb_valides) + hors_plage

    def deux_colonnes_hors_plage(self, numero):
        """Une seule ligne qui viole DEUX colonnes avec le même code HORS_PLAGE."""
        return self._avec_cle(
            numero,
            quantite_produite=self._borne_haute("quantite_produite") + 1,
            **{COLONNE_DECIMALE_BORNEE: self._borne_haute(COLONNE_DECIMALE_BORNEE) + 1},
        )

    def deux_codes_differents(self, numero):
        """Une seule ligne qui porte DEUX codes différents : HORS_PLAGE (température) et VALEUR_HORS_DOMAINE (ligne de fabrication)."""
        return dict(self.hors_plage(numero), id_ligne=VALEUR_HORS_DOMAINE)

    def hors_domaine(self, numero):
        """Violation VALEUR_HORS_DOMAINE : une ligne de fabrication inconnue."""
        return self._avec_cle(numero, id_ligne=VALEUR_HORS_DOMAINE)

    def concurrente(self, ligne):
        """Même clé primaire, contenu différent mais valide : provoque un CLE_EN_CONFLIT."""
        return dict(ligne, quantite_produite=ligne["quantite_produite"] + 1)


@pytest.fixture()
def fabrique(contrat, ligne_conforme):
    return FabriqueDeLignes(contrat, ligne_conforme)
