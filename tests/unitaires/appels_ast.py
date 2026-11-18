"""Les appels de fonction d'un code source, lus dans son arbre syntaxique : l'outil des gardes « ce module ne réécrit pas telle étape ».

Partagé par `test_passage.py` (le programme appelle `passer`, il ne réécrit rien) et `test_ingestion.py` (l'ingestion non plus). Un appel
se lit par son NOM : `nom(...)` ou `objet.nom(...)`, ramené à celui d'origine quand il est connu sous un alias (`from m import a as b`
ou `b = a`, en chaîne, sans boucler sur un cycle). Ce qu'une lecture par noms ne voit pas : une écriture directe dans un magasin, un
`getattr`, une fonction reçue en paramètre.
"""
import ast


def alias(arbre):
    """Les noms sous lesquels une fonction est aussi connue dans le module : `from m import a as b` et `b = a` (b -> a)."""
    noms = {}
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.ImportFrom):
            noms |= {nom.asname: nom.name for nom in noeud.names if nom.asname}
        elif isinstance(noeud, ast.Assign) and isinstance(noeud.value, ast.Name):
            noms |= {cible.id: noeud.value.id for cible in noeud.targets if isinstance(cible, ast.Name)}
    return noms


def nom_d_origine(nom, noms_connus):
    """Le nom qu'une fonction porte à l'origine, en suivant les alias (un cycle s'arrête au lieu de boucler)."""
    vus = set()
    while nom in noms_connus and nom not in vus:
        vus.add(nom)
        nom = noms_connus[nom]
    return nom


def _noms_appeles(noeud, noms_connus):
    noms = set()
    for appel in ast.walk(noeud):
        if isinstance(appel, ast.Call):
            cible = appel.func
            nom = cible.id if isinstance(cible, ast.Name) else cible.attr if isinstance(cible, ast.Attribute) else ""
            noms.add(nom_d_origine(nom, noms_connus))
    return noms


def appels_directs(source, fonction):
    """Les noms d'origine de tout ce que `fonction` appelle (`nom(...)` ou `objet.nom(...)`), fonctions imbriquées et alias compris."""
    arbre = ast.parse(source)
    noeud = next(n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef) and n.name == fonction)
    return _noms_appeles(noeud, alias(arbre))


def appels_du_module(source):
    """Les noms d'origine de tout ce que le module appelle, dans ses fonctions, ses classes et hors de toute fonction."""
    arbre = ast.parse(source)
    return _noms_appeles(arbre, alias(arbre))
