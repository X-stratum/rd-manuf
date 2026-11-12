"""Génère `docs/plan-global.svg`, la feuille « plan global » affichée en haut du README.

Pour changer l'avancement : modifier les statuts dans les listes COLONNES, REGLES ou OUTILS ci-dessous, puis lancer

    python docs/generer_plan_global.py

et commiter l'image régénérée. Un test (`tests/unitaires/test_plan_global.py`) vérifie que l'image commitée correspond au générateur.
Le SVG est statique et sans police web : c'est ce qui le rend affichable tel quel par GitHub.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

SORTIE = Path(__file__).with_name("plan-global.svg")
NUMERO_REVISION = 4
DATE_REVISION = "2026-11-12"

# ==============================================================================
# 📋 LE PLAN : c'est ici qu'on change un statut
# ==============================================================================
FAIT, PROCHAIN, A_VENIR = "fait", "prochain", "avenir"

COLONNES = [
    ("01", "RECEVOIR", [
        ("Aiguillage", ["Choisit le lecteur selon", "le format du fichier"], FAIT),
        ("Lecture", ["CSV et Parquet ; les", "fichiers abîmés refusés"], FAIT),
        ("Nom du fichier", ["Pays, usine et date en", "UTC, forme contrôlée"], FAIT)]),
    ("02", "CONTRÔLER", [
        ("Structure", ["Colonnes manquantes,", "inconnues ou en double"], FAIT),
        ("Jugement des lignes", ["Chaque ligne contre le", "contrat ; seuil de rejet"], FAIT)]),
    ("03", "DÉCIDER", [
        ("Décision", ["Acceptée, avec rejets,", "bloquée ou refusée"], FAIT),
        ("Registre", ["Ligne par livraison faite ;", "table à écrire"], PROCHAIN)]),
    ("04", "STOCKER", [
        ("Bronze", ["Données telles que", "reçues, conservées"], PROCHAIN),
        ("Silver", ["Données contrôlées ;", "rejets en quarantaine"], A_VENIR),
        ("Gold", ["Indicateurs prêts", "pour le métier"], A_VENIR)]),
    ("05", "PRÉVENIR", [
        ("Routage d'alertes", ["Qui agit et qui est", "informé, par rôles"], FAIT),
        ("Annuaire", ["Contacts gardés hors", "du dépôt public"], PROCHAIN),
        ("Envoi", ["Prévient réellement", "les bonnes personnes"], A_VENIR)]),
    ("06", "LIVRER", [
        ("Déploiement dev", ["Bundle écrit et validé ;", "rien de déployé"], A_VENIR),
        ("Mise en production", ["Version 0.1.0, puis", "reprise sur incident"], A_VENIR)]),
]
ACTEURS = [  # (numéro de la colonne au-dessus de laquelle la carte se place, titre, lignes)
    (0, "Usine émettrice", ["Envoie les fichiers ;", "agit sur alerte"]),
    (3, "Métier", ["Utilise les données ;", "toujours informé"]),
    (4, "Équipe data", ["Construit le pipeline ;", "informée des refus"]),
]
REGLES = [  # (numéro de la colonne alignée, titre, lignes) : écrites, donc faites
    (1, "Contrat de données", ["Colonnes, types, limites"]),
    (2, "Codes d'erreur", ["Stables, jamais renommés"]),
    (4, "Catalogue d'alertes", ["Qui agit, qui est informé"]),
]
OUTILS = [  # (titre, lignes) : faits
    ("Simulateur", ["Livraisons de test avec", "anomalies à la demande"]),
    ("Tests unitaires", ["Écrits avant le code,", "sabotage pour les prouver"]),
    ("CI GitHub", ["Style puis tests à", "chaque Pull Request"]),
    ("Journal des changements", ["Le projet raconté en", "langage clair"]),
    ("Revue et squash", ["Modèle de PR, revue,", "historique lisible"]),
]


def _tous_les_statuts() -> list[str]:
    statuts = [statut for _, _, cartes in COLONNES for _, _, statut in cartes]
    return statuts + [FAIT] * (len(REGLES) + len(OUTILS))


def nb_blocs() -> int:
    return len(_tous_les_statuts())


def nb_blocs_faits() -> int:
    return _tous_les_statuts().count(FAIT)


# ==============================================================================
# 🎨 STYLE
# ==============================================================================
INK, MUTED, LINE = "#1b2733", "#5b6b7a", "#cfd8de"
TEAL, TEAL_T, TEAL_B = "#1f7a6d", "#eaf5f2", "#86bdb2"
ORANGE, ORANGE_T, ORANGE_B = "#b8651b", "#fdf2e6", "#e3a971"
SLATE, SLATE_B = "#6b7a88", "#b4bfc9"
BLUE, BLUE_T, BLUE_B = "#2f5fb3", "#e8eef9", "#9db6e0"
PURPLE = "#5b3fb0"
POLICE = "'Segoe UI','Helvetica Neue',Arial,sans-serif"
CONDENSEE = "'Bebas Neue','Oswald','Arial Narrow','Helvetica Neue Condensed',Impact,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono',monospace"

# statut -> (étiquette, couleur du texte, fond de la carte, couleur de la bordure, pointillés)
STYLE_STATUT = {
    FAIT: ("FAIT", TEAL, TEAL_T, TEAL_B, None),
    PROCHAIN: ("PROCHAIN", ORANGE, ORANGE_T, ORANGE_B, None),
    A_VENIR: ("À VENIR", SLATE, "#ffffff", SLATE_B, "5 4"),
}

# géométrie (en pixels du SVG)
LARGEUR, HAUTEUR = 1400, 920
X_COLONNES, LARGEUR_COLONNE, ECART = 176, 186, 13
Y_ACTEURS, Y_CHAINE, Y_REGLES, Y_OUTILS = 196, 306, 656, 760
H_COULOIR, H_CHAINE = 90, 330
H_CARTE_PRINCIPALE, H_CARTE_COMPACTE = 84, 68


def _x_colonne(i: int) -> float:
    return X_COLONNES + i * (LARGEUR_COLONNE + ECART)


def _milieu_colonne(i: int) -> float:
    return _x_colonne(i) + LARGEUR_COLONNE / 2


# ==============================================================================
# ✏️ PRIMITIVES SVG
# ==============================================================================
class _Dessin:
    def __init__(self) -> None:
        self.lignes: list[str] = []

    def texte(self, x, y, contenu, taille=12, couleur=INK, graisse=400, ancre="start", police=POLICE, espacement=None) -> None:
        sp = f' letter-spacing="{espacement}"' if espacement is not None else ""
        self.lignes.append(
            f'<text x="{x}" y="{y}" font-family="{police}" font-size="{taille}" font-weight="{graisse}" '
            f'fill="{couleur}" text-anchor="{ancre}"{sp}>{escape(contenu)}</text>'
        )

    def rect(self, x, y, w, h, fond, bord=None, rayon=8, pointilles=None, epaisseur=1) -> None:
        trait = f' stroke="{bord}" stroke-width="{epaisseur}"' if bord else ""
        points = f' stroke-dasharray="{pointilles}"' if pointilles else ""
        self.lignes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rayon}" fill="{fond}"{trait}{points}/>')

    def fleche(self, x1, y1, x2, y2, couleur, marqueur, pointilles=None) -> None:
        points = f' stroke-dasharray="{pointilles}"' if pointilles else ""
        self.lignes.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{couleur}" stroke-width="2"{points} marker-end="url(#{marqueur})"/>'
        )

    def brut(self, contenu: str) -> None:
        self.lignes.append(contenu)

    def carte(self, x, y, w, h, titre, lignes, statut=None, acteur=False, barre=True) -> None:
        if acteur:
            etiquette, couleur, fond, bord, pointilles = "ACTEUR", BLUE, BLUE_T, BLUE_B, None
        else:
            etiquette, couleur, fond, bord, pointilles = STYLE_STATUT[statut]
        grande = h > 70
        self.rect(x, y, w, h, fond, bord, 8, pointilles, 1.2)
        self.texte(x + w - 10, y + 16, etiquette, 8.5, couleur, 700, "end", MONO, 0.6)
        self.texte(x + 12, y + (37 if grande else 34), titre, 13.5, INK, 700)
        for rang, ligne in enumerate(lignes):
            self.texte(x + 12, y + (53 if grande else 49) + 13 * rang, ligne, 10, MUTED, 400, "start", MONO)
        if barre and not acteur:
            self.rect(x + 12, y + h - 11, w - 24, 4, "#e1e7ec", None, 2)
            if statut == FAIT:
                self.rect(x + 12, y + h - 11, w - 24, 4, TEAL, None, 2)

    def couloir(self, y, titre, sous_titre) -> None:
        self.rect(38, y, LARGEUR - 76, H_COULOIR if y != Y_CHAINE else H_CHAINE, "#f8fafb", LINE, 10)
        self.texte(52, y + 34, titre, 20, INK, 800, "start", CONDENSEE, 0.5)
        for rang, ligne in enumerate(sous_titre):
            self.texte(52, y + 52 + 13 * rang, ligne, 9.5, MUTED, 400, "start", MONO)


# ==============================================================================
# 🧱 CONSTRUCTION DE LA FEUILLE
# ==============================================================================
def _entete(d: _Dessin, faits: int, total: int) -> None:
    d.texte(40, 36, "RD-MANUF  ·  R&D MANUFACTURING  ·  DATABRICKS", 11, MUTED, 600, "start", MONO, 2)
    d.texte(40, 90, "PLAN GLOBAL", 60, INK, 800, "start", CONDENSEE, -0.5)
    d.texte(40, 112, "Du fichier envoyé par l'usine aux données prêtes à l'emploi : "
                     "ce qui est fait, ce qui vient ensuite, ce qui reste à construire.", 13.5, MUTED)
    d.texte(LARGEUR - 40, 78, f"{faits} / {total}", 46, TEAL, 800, "end", CONDENSEE)
    d.texte(LARGEUR - 40, 98, "BLOCS TERMINÉS", 10, MUTED, 600, "end", MONO, 1.5)
    d.rect(LARGEUR - 340, 106, 300, 8, "#dde4ea", None, 4)
    d.rect(LARGEUR - 340, 106, 300 * faits / total, 8, TEAL, None, 4)


def _legende(d: _Dessin) -> None:
    d.rect(24, 128, LARGEUR - 48, 764, "#ffffff", LINE, 14, None, 1.2)
    d.texte(44, 168, "STATUT", 9.5, MUTED, 700, "start", MONO, 1.5)
    x = 108
    for statut, nom in ((FAIT, "Fait"), (PROCHAIN, "Prochain (étape en cours)"), (A_VENIR, "À venir")):
        _, _, fond, bord, pointilles = STYLE_STATUT[statut]
        d.rect(x, 157, 18, 14, fond, bord, 4, pointilles, 1.4)
        d.texte(x + 26, 168, nom, 11.5, INK, 500)
        x += 26 + len(nom) * 6.6 + 26
    d.texte(560, 168, "FLUX", 9.5, MUTED, 700, "start", MONO, 1.5)
    flux = (("Données", TEAL, None, "fd"), ("Alertes", ORANGE, None, "fa"), ("Règles écrites", PURPLE, "5 4", "fr"))
    for rang, (nom, couleur, pointilles, marqueur) in enumerate(flux):
        x_flux = 606 + rang * 150
        d.fleche(x_flux, 164, x_flux + 30, 164, couleur, marqueur, pointilles)
        d.texte(x_flux + 40, 168, nom, 11.5, INK, 500)
    d.brut(f'<line x1="44" y1="184" x2="{LARGEUR - 44}" y2="184" stroke="{LINE}" stroke-width="1"/>')


def _chaine(d: _Dessin) -> None:
    for i, (numero, nom, cartes) in enumerate(COLONNES):
        x = _x_colonne(i)
        d.rect(x, Y_CHAINE + 8, LARGEUR_COLONNE, H_CHAINE - 16, "#eef3f7", LINE, 10)
        d.texte(x + 12, Y_CHAINE + 33, numero, 10, MUTED, 600, "start", MONO)
        d.texte(x + 30, Y_CHAINE + 34, nom, 15, INK, 800, "start", CONDENSEE, 0.2)
        faits = sum(1 for _, _, statut in cartes if statut == FAIT)
        couleur_compteur = TEAL if faits == len(cartes) else MUTED
        d.texte(x + LARGEUR_COLONNE - 12, Y_CHAINE + 33, f"{faits}/{len(cartes)}", 10, couleur_compteur, 700, "end", MONO)
        for rang, (titre, lignes, statut) in enumerate(cartes):
            d.carte(x + 8, Y_CHAINE + 46 + rang * 92, LARGEUR_COLONNE - 16, H_CARTE_PRINCIPALE, titre, lignes, statut)
        if i < len(COLONNES) - 1:        # petit triangle de lecture entre deux colonnes
            milieu = x + LARGEUR_COLONNE + ECART / 2
            haut, bas = Y_CHAINE + 28, Y_CHAINE + 38
            d.brut(f'<path d="M{milieu - 4:.1f} {haut} L{milieu + 4:.1f} {Y_CHAINE + 33} L{milieu - 4:.1f} {bas}z" fill="{TEAL}"/>')


def _acteurs_et_regles(d: _Dessin) -> None:
    for colonne, titre, lignes in ACTEURS:
        d.carte(_x_colonne(colonne), Y_ACTEURS + 11, LARGEUR_COLONNE, H_CARTE_COMPACTE, titre, lignes, acteur=True)
    for colonne, titre, lignes in REGLES:
        d.carte(_x_colonne(colonne), Y_REGLES + 11, LARGEUR_COLONNE, H_CARTE_COMPACTE, titre, lignes, FAIT, barre=False)
    d.fleche(_milieu_colonne(0), Y_ACTEURS + 80, _milieu_colonne(0), Y_CHAINE + 6, TEAL, "fd")       # l'usine envoie
    d.fleche(_milieu_colonne(3), Y_CHAINE + 6, _milieu_colonne(3), Y_ACTEURS + 82, TEAL, "fd")       # le métier utilise le Gold
    d.fleche(_milieu_colonne(4), Y_CHAINE + 6, _milieu_colonne(4), Y_ACTEURS + 82, ORANGE, "fa")     # l'équipe data est informée
    for colonne, _, _ in REGLES:
        d.fleche(_milieu_colonne(colonne), Y_REGLES + 11, _milieu_colonne(colonne), Y_CHAINE + H_CHAINE - 4, PURPLE, "fr", "5 4")
    for colonne, mot, couleur in ((0, "livraison", TEAL), (3, "données", TEAL), (4, "alertes", ORANGE)):
        d.texte(_milieu_colonne(colonne) + 10, Y_ACTEURS + 102, mot, 9, couleur, 600, "start", MONO)


def _outils(d: _Dessin) -> None:
    largeur = (1181 - 4 * 15) / 5
    for rang, (titre, lignes) in enumerate(OUTILS):
        d.carte(X_COLONNES + rang * (largeur + 15), Y_OUTILS + 11, largeur, H_CARTE_COMPACTE, titre, lignes, FAIT, barre=False)


def _pied(d: _Dessin, total: int) -> None:
    y = 862
    d.brut(f'<line x1="44" y1="{y}" x2="{LARGEUR - 44}" y2="{y}" stroke="{LINE}" stroke-width="1"/>')
    d.texte(52, y + 20, "FEUILLE 1/1", 9.5, MUTED, 600, "start", MONO, 1.5)
    d.texte(160, y + 20, "PLAN GLOBAL", 9.5, MUTED, 600, "start", MONO, 1.5)
    d.texte(280, y + 20, f"{total} BLOCS  ·  3 STATUTS  ·  3 TYPES DE FLUX  ·  6 ÉTAPES", 9.5, INK, 700, "start", MONO, 1.5)
    d.texte(LARGEUR - 52, y + 20, f"RÉV. {NUMERO_REVISION}  ·  {DATE_REVISION}", 9.5, MUTED, 600, "end", MONO, 1.5)


def construire_svg() -> str:
    faits, total = nb_blocs_faits(), nb_blocs()
    d = _Dessin()
    d.brut(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {LARGEUR} {HAUTEUR}" width="{LARGEUR}" height="{HAUTEUR}" '
           'role="img" aria-labelledby="t d">')
    d.brut('<title id="t">rd-manuf : plan global</title>')
    d.brut(f'<desc id="d">Du fichier envoyé par l\'usine aux données prêtes à l\'emploi : {faits} blocs terminés sur {total}, six étapes '
           '(recevoir, contrôler, décider, stocker, prévenir, livrer), les règles écrites et les outils.</desc>')
    d.brut('<defs>')
    d.brut('<pattern id="grille" width="24" height="24" patternUnits="userSpaceOnUse">'
           '<path d="M24 0H0V24" fill="none" stroke="#e6ebef" stroke-width="1"/></pattern>')
    for nom, couleur in (("d", TEAL), ("a", ORANGE), ("r", PURPLE)):
        d.brut(f'<marker id="f{nom}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
               f'<path d="M1 1L9 5L1 9z" fill="{couleur}"/></marker>')
    d.brut('</defs>')
    d.rect(0, 0, LARGEUR, HAUTEUR, "#f6f8fa", None, 0)
    d.rect(0, 0, LARGEUR, HAUTEUR, "url(#grille)", None, 0)
    _entete(d, faits, total)
    _legende(d)
    d.couloir(Y_ACTEURS, "ACTEURS", ("qui envoie,", "qui agit, qui sait"))
    d.couloir(Y_CHAINE, "CHAÎNE", ("de la livraison", "au tableau de bord"))
    d.couloir(Y_REGLES, "RÈGLES", ("écrites, versionnées,", "lisibles par le métier"))
    d.couloir(Y_OUTILS, "OUTILS", ("preuves et", "processus d'équipe"))
    _chaine(d)
    _acteurs_et_regles(d)
    _outils(d)
    _pied(d, total)
    d.brut('</svg>')
    return "\n".join(d.lignes) + "\n"


if __name__ == "__main__":
    SORTIE.write_text(construire_svg(), encoding="utf-8")
    print(f"écrit {SORTIE.name} : {nb_blocs_faits()}/{nb_blocs()} blocs terminés")
