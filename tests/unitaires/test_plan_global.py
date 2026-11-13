"""Le plan global (`docs/plan-global.svg`) est GÉNÉRÉ par `docs/generer_plan_global.py`.

L'image commitée ne doit jamais dériver de son générateur : sans ce test, on pourrait changer un statut dans le générateur
sans regénérer l'image (ou l'inverse), et le README afficherait un plan faux.
"""
import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
SVG = RACINE / "docs" / "plan-global.svg"
GENERATEUR = RACINE / "docs" / "generer_plan_global.py"
README = RACINE / "README.md"
SRC = RACINE / "src" / "rd_manuf"
EMOJI = {"fait": "🟢", "prochain": "🟠", "avenir": "⚪"}
LONGUEUR_MAX_PUCE = 220          # une puce du plan résume : le détail est dans le code, les tests et le CHANGELOG


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


# ==============================================================================
# 🔒 GARDES : le README et le plan ne doivent pas diverger (la CI échoue si on oublie l'un des deux)
# ==============================================================================
def _plan_du_readme() -> str:
    texte = README.read_text(encoding="utf-8")
    return texte[texte.index("## Plan global"):texte.index("## Environnements")]


def _puces(texte: str) -> dict[str, tuple[str, str]]:
    """Les puces `- 🟢 **Nom** : …` d'un texte : nom -> (pastille, ligne entière)."""
    trouvees = {}
    for ligne in texte.splitlines():
        m = re.match(r"- (🟢|🟠|⚪) \*\*(.+?)\*\*", ligne)
        if m:
            trouvees[m.group(2)] = (m.group(1), ligne)
    return trouvees


def test_chaque_bloc_de_l_image_a_sa_puce_au_meme_statut_dans_le_readme():
    """Les 15 blocs des colonnes : même nom, même statut dans l'image et dans le README, et aucune puce sans bloc."""
    plan_des_colonnes = _plan_du_readme().split("Règles écrites et outils")[0]
    puces = _puces(plan_des_colonnes)
    attendu = {nom: EMOJI[statut] for _, _, blocs in _generateur().COLONNES for nom, _, statut in blocs}

    sans_puce, sans_bloc = sorted(set(attendu) - set(puces)), sorted(set(puces) - set(attendu))
    assert (sans_puce, sans_bloc) == ([], []), f"blocs sans puce : {sans_puce} ; puces sans bloc : {sans_bloc}"
    assert {nom: pastille for nom, (pastille, _) in puces.items()} == attendu


def test_chaque_module_du_code_est_decrit_dans_l_organisation_du_depot():
    texte = README.read_text(encoding="utf-8")
    modules = [fichier.name for fichier in sorted(SRC.glob("*.py")) if fichier.name != "__init__.py"]

    assert [nom for nom in modules if f"`{nom}`" not in texte] == []


def test_les_liens_du_readme_pointent_vers_des_fichiers_qui_existent():
    liens = re.findall(r"\]\(([^)#]+)\)", README.read_text(encoding="utf-8"))
    relatifs = {lien for lien in liens if not lien.startswith("http")}

    assert sorted(lien for lien in relatifs if not (RACINE / lien).exists()) == []


def test_les_puces_du_plan_restent_synthetiques():
    trop_longues = {nom: len(ligne) for nom, (_, ligne) in _puces(_plan_du_readme()).items() if len(ligne) > LONGUEUR_MAX_PUCE}

    assert trop_longues == {}, f"puces de plus de {LONGUEUR_MAX_PUCE} caractères : {trop_longues}"
