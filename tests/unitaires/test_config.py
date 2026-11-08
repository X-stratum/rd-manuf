"""Tests des garde-fous d'environnement : ce sont eux qui protègent la prod d'une erreur de paramètre."""
import pytest

from rd_manuf.config import (
    CATALOGUE_PAR_DEFAUT,
    CATALOGUE_PAR_ENVIRONNEMENT,
    ENV_DEV,
    ENV_PLAYGROUND,
    ENV_PROD,
    ErreurEnvironnement,
    environnement_du_catalogue,
)


def test_le_catalogue_par_defaut_est_le_playground():
    """Un paramètre oublié ne doit jamais toucher la prod."""
    assert environnement_du_catalogue(CATALOGUE_PAR_DEFAUT) == ENV_PLAYGROUND


@pytest.mark.parametrize("environnement", [ENV_PLAYGROUND, ENV_DEV, ENV_PROD])
def test_chaque_catalogue_connu_retrouve_son_environnement(environnement):
    assert environnement_du_catalogue(CATALOGUE_PAR_ENVIRONNEMENT[environnement]) == environnement


@pytest.mark.parametrize("catalogue", ["space_1", "rd_manuf_prod ", "RD_MANUF_PROD", "", "main"])
def test_un_catalogue_inconnu_est_refuse(catalogue):
    """Refuser l'inconnu : un nom mal orthographié ne doit jamais être traité « par défaut »."""
    with pytest.raises(ErreurEnvironnement):
        environnement_du_catalogue(catalogue)
