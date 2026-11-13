"""Le plan global (`docs/plan-global.svg`) est GÉNÉRÉ par `docs/generer_plan_global.py`.

L'image commitée ne doit jamais dériver de son générateur : sans ce test, on pourrait changer un statut dans le générateur
sans regénérer l'image (ou l'inverse), et le README afficherait un plan faux.
"""
import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

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
def _section_du_readme(titre: str) -> str:
    """Le texte d'une section `## titre` du README, jusqu'à la suivante ; échoue clairement si le titre a changé."""
    texte = README.read_text(encoding="utf-8")
    assert f"## {titre}" in texte, f"la section « {titre} » est introuvable dans le README"
    debut = texte.index(f"## {titre}")
    fin = texte.find("\n## ", debut + 1)
    return texte[debut:] if fin == -1 else texte[debut:fin]


def _puces(texte: str) -> dict[str, tuple[str, str]]:
    """Les puces `- 🟢 **Nom** : …` d'un texte : nom -> (pastille, ligne entière)."""
    trouvees = {}
    for ligne in texte.splitlines():
        m = re.match(r"- (🟢|🟠|⚪) \*\*(.+?)\*\*", ligne)
        if m:
            trouvees[m.group(2)] = (m.group(1), ligne)
    return trouvees


def test_chaque_bloc_de_l_image_a_sa_puce_au_meme_statut_dans_le_readme():
    """Les blocs des colonnes : même nom, même statut dans l'image et dans le README, et aucune puce sans bloc."""
    plan_des_colonnes = _section_du_readme("Plan global").split("Règles écrites et outils")[0]
    puces = _puces(plan_des_colonnes)
    attendu = {nom: EMOJI[statut] for _, _, blocs in _generateur().COLONNES for nom, _, statut in blocs}

    sans_puce, sans_bloc = sorted(set(attendu) - set(puces)), sorted(set(puces) - set(attendu))
    assert (sans_puce, sans_bloc) == ([], []), f"blocs sans puce : {sans_puce} ; puces sans bloc : {sans_bloc}"
    assert {nom: pastille for nom, (pastille, _) in puces.items()} == attendu


def test_chaque_module_du_code_est_decrit_dans_l_organisation_du_depot():
    organisation = _section_du_readme("Organisation du dépôt")      # pas tout le README : cité dans le plan seulement, ça ne compte pas
    modules = [fichier.name for fichier in sorted(SRC.glob("*.py")) if fichier.name != "__init__.py"]

    assert [nom for nom in modules if f"`{nom}`" not in organisation] == []


def test_les_liens_du_readme_pointent_vers_des_fichiers_qui_existent():
    liens = re.findall(r"\]\(([^)#]+)\)", README.read_text(encoding="utf-8"))
    relatifs = {lien for lien in liens if not lien.startswith("http")}

    assert sorted(lien for lien in relatifs if not (RACINE / lien).exists()) == []


def test_les_puces_du_plan_restent_synthetiques():
    puces = _puces(_section_du_readme("Plan global"))
    trop_longues = {nom: len(ligne) for nom, (_, ligne) in puces.items() if len(ligne) > LONGUEUR_MAX_PUCE}

    assert trop_longues == {}, f"puces de plus de {LONGUEUR_MAX_PUCE} caractères : {trop_longues}"


# ==============================================================================
# 📐 GARDES DE GÉOMÉTRIE : l'image ne doit jamais déborder (la CI ne « voit » pas le rendu : ces tests le calculent)
# ==============================================================================
# Largeur d'un caractère, estimée : un titre est en gras à 13,5 px (environ 7,4 px par caractère), une ligne de description en
# monospace à 10 px (0,6 em, soit 6 px). C'est une HEURISTIQUE, pas une mesure : aucun moteur de rendu n'est disponible dans les tests.
PX_PAR_CARACTERE_TITRE = 7.4
PX_PAR_CARACTERE_LIGNE = 6.0
MARGE_INTERIEURE_CARTE = 12 + 10           # le texte commence à 12 px du bord gauche, et on garde 10 px à droite
MARGE_DU_COULOIR = 38                       # les couloirs s'étendent de 38 px à LARGEUR - 38


def test_les_colonnes_de_la_chaine_tiennent_dans_le_couloir():
    g = _generateur()
    n = len(g.COLONNES)
    bord_droit = g.X_COLONNES + (n - 1) * (g.LARGEUR_COLONNE + g.ECART) + g.LARGEUR_COLONNE

    limite = g.LARGEUR - MARGE_DU_COULOIR
    assert bord_droit <= limite, f"{n} colonnes : bord droit à {bord_droit} px, couloir jusqu'à {limite} px"


def test_la_derniere_carte_de_chaque_colonne_tient_dans_sa_colonne():
    g = _generateur()
    bas_de_la_colonne = g.Y_CHAINE + 8 + g.H_CHAINE - 16
    debordements = {
        nom: g.Y_CHAINE + 46 + (len(cartes) - 1) * g.PAS_CARTE + g.H_CARTE_PRINCIPALE
        for _, nom, cartes in g.COLONNES
        if g.Y_CHAINE + 46 + (len(cartes) - 1) * g.PAS_CARTE + g.H_CARTE_PRINCIPALE > bas_de_la_colonne
    }

    assert debordements == {}, f"cartes qui dépassent le bas de leur colonne ({bas_de_la_colonne} px) : {debordements}"


def test_les_titres_et_les_descriptions_tiennent_dans_leur_carte():
    """Estimation : voir les constantes ci-dessus. Un titre trop long déborde de sa carte sans rien signaler."""
    g = _generateur()
    place = (g.LARGEUR_COLONNE - 16) - MARGE_INTERIEURE_CARTE
    trop_larges = {}
    for _, _, cartes in g.COLONNES:
        for titre, lignes, _ in cartes:
            if len(titre) * PX_PAR_CARACTERE_TITRE > place:
                trop_larges[titre] = round(len(titre) * PX_PAR_CARACTERE_TITRE)
            for ligne in lignes:
                if len(ligne) * PX_PAR_CARACTERE_LIGNE > place:
                    trop_larges[ligne] = round(len(ligne) * PX_PAR_CARACTERE_LIGNE)

    assert trop_larges == {}, f"texte estimé plus large que la place ({place:.0f} px) : {trop_larges}"


def test_la_rangee_des_outils_a_la_meme_largeur_que_la_chaine():
    """Les outils (autant d'espaces que d'intervalles) occupent la largeur des colonnes, quel que soit leur nombre."""
    g = _generateur()
    largeur_de_la_rangee = len(g.OUTILS) * g.LARGEUR_OUTIL + (len(g.OUTILS) - 1) * g.ESPACE_OUTILS

    assert largeur_de_la_rangee == pytest.approx(g.LARGEUR_UTILE)
    assert g.X_COLONNES + largeur_de_la_rangee <= g.LARGEUR - MARGE_DU_COULOIR + 1e-6


# ==============================================================================
# 🔒 Le nombre d'étapes est écrit en trois endroits : il doit suivre le nombre de colonnes du plan
# ==============================================================================
NOMBRES_EN_LETTRES = {5: "cinq", 6: "six", 7: "sept", 8: "huit", 9: "neuf", 10: "dix"}


def _en_lettres(nombre: int) -> str:
    assert nombre in NOMBRES_EN_LETTRES, f"{nombre} étapes : ajouter ce nombre en lettres à NOMBRES_EN_LETTRES"
    return NOMBRES_EN_LETTRES[nombre]


def test_le_pied_de_l_image_annonce_autant_d_etapes_que_de_colonnes():
    annonce = re.search(r"(\d+) ÉTAPES", SVG.read_text(encoding="utf-8"))

    assert annonce, "le pied de l'image ne contient plus « N ÉTAPES » : adapter ce test"
    assert int(annonce.group(1)) == len(_generateur().COLONNES)


def test_la_description_de_l_image_nomme_toutes_les_etapes():
    description = ET.fromstring(SVG.read_text(encoding="utf-8")).find("{http://www.w3.org/2000/svg}desc").text
    colonnes = _generateur().COLONNES
    annonce = re.search(r"(\w+) étapes", description)

    assert annonce, "la description de l'image ne contient plus « N étapes » : adapter ce test"
    assert annonce.group(1) == _en_lettres(len(colonnes))
    absentes = [nom.lower() for _, nom, _ in colonnes if nom.lower() not in description]
    assert absentes == [], f"étapes du plan absentes de la description de l'image : {absentes}"


def test_le_texte_alternatif_de_l_image_du_readme_annonce_autant_d_etapes_que_de_colonnes():
    alt = re.search(r'<img src="docs/plan-global\.svg" alt="([^"]*)"', README.read_text(encoding="utf-8"))
    assert alt, "l'image du plan n'a plus de texte alternatif dans le README : adapter ce test"
    annonce = re.search(r"(\w+) étapes", alt.group(1))

    assert annonce, "le texte alternatif ne contient plus « N étapes » : adapter ce test"
    assert annonce.group(1) == _en_lettres(len(_generateur().COLONNES))
