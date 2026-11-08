"""Fixtures partagées : on teste le VRAI contrat du dépôt, pas une copie."""
from pathlib import Path

import pytest

from rd_manuf.contrat import charger_contrat

RACINE_DEPOT = Path(__file__).resolve().parents[2]
CHEMIN_CONTRAT = RACINE_DEPOT / "contracts" / "data" / "livraison_production.yaml"


@pytest.fixture(scope="session")
def contrat():
    return charger_contrat(CHEMIN_CONTRAT)


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
