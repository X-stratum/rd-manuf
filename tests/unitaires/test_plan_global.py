"""Le plan global (`docs/plan-global.svg`) est GÉNÉRÉ par `docs/generer_plan_global.py`.

L'image commitée ne doit jamais dériver de son générateur : sans ce test, on pourrait changer un statut dans le générateur
sans regénérer l'image (ou l'inverse), et le README afficherait un plan faux.
"""
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
SVG = RACINE / "docs" / "plan-global.svg"
GENERATEUR = RACINE / "docs" / "generer_plan_global.py"


def _generateur():
    spec = importlib.util.spec_from_file_location("generer_plan_global", GENERATEUR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_l_image_commitee_correspond_a_son_generateur():
    assert SVG.read_text(encoding="utf-8") == _generateur().construire_svg(), "relancer : python docs/generer_plan_global.py"


def test_l_image_est_un_svg_valide():
    racine = ET.fromstring(SVG.read_text(encoding="utf-8"))
    assert racine.tag == "{http://www.w3.org/2000/svg}svg"


def test_le_compteur_de_blocs_est_coherent():
    generateur = _generateur()
    assert 0 < generateur.nb_blocs_faits() <= generateur.nb_blocs()
