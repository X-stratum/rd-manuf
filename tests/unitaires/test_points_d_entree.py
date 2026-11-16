"""Les programmes qu'on lance avec `python -m rd_manuf.<nom>` : le README les documente, la dernière ligne de chacun les branche.

`principal` est testée à part, avec de faux services. Ce qui reste sans test sans ceci, c'est le branchement lui-même (`if __name__ ==
"__main__"`), qu'un renommage de `principal` casserait sans qu'aucun test le voie.
"""
import runpy
import sys

import pytest

PROGRAMMES = ["rd_manuf.passage", "rd_manuf.sondage"]


@pytest.mark.parametrize("module", PROGRAMMES)
def test_lancer_un_programme_en_ligne_de_commande_affiche_son_aide_et_sort_proprement(module, monkeypatch, capsys):
    """`--help` ne se connecte à rien : la sortie est 0 et l'aide s'affiche, donc le branchement appelle bien `principal`."""
    monkeypatch.delitem(sys.modules, module, raising=False)         # runpy relit le module comme programme principal
    monkeypatch.setattr(sys, "argv", [module, "--help"])

    with pytest.raises(SystemExit) as sortie:
        runpy.run_module(module, run_name="__main__")

    assert sortie.value.code == 0
    assert capsys.readouterr().out.strip() != ""
