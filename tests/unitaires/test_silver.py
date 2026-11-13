"""Tests du SILVER d'une livraison : les lignes valides typées, et la quarantaine des lignes rejetées.

Le silver REJUGE le texte du bronze avec le contrat (`valider_livraison`, comme la réception) : une seule source de vérité.
Sa garantie centrale : pour une même version de contrat, il décide comme la réception (statut du registre), sinon une erreur claire.
Une livraison bloquée, refusée ou rejouée ne rend rien ; la quarantaine ne sert qu'aux livraisons acceptées avec rejets, lignes coupées
comprises. Ces tests ne touchent aucune table : le module construit des lignes.
"""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf.bronze import LigneBronze, construire_lignes_bronze, construire_lignes_coupees
from rd_manuf.contrat import CodeErreur
from rd_manuf.lecture import lire_csv, lire_selon_format
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.registre import LigneRegistre, traiter_fichier
from rd_manuf.silver import LigneQuarantaine, LigneSilver, ResultatSilver, construire_silver
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
NB_LIGNES = 20
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
NOM = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
NOM_PARQUET = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.parquet"
RIEN = ResultatSilver(lignes=[], quarantaine=[])


def _ecrire(tmp_path, contrat, anomalies=None, nb_lignes=NB_LIGNES, nom=NOM):
    chemin = tmp_path / nom
    ecrire_livraison_csv(contrat, generer_livraison(contrat, nb_lignes, GRAINE, anomalies), chemin)
    return chemin


def _ecrire_csv_avec_lignes_coupees(tmp_path, entete):
    """3 lignes bien formées (lignes 2, 4 et 6 du fichier) et 2 lignes coupées (lignes 3 et 5) ; l'en-tête est la ligne 1."""
    chemin = tmp_path / NOM
    lignes = [
        ",".join(entete),
        "LOT-000001,L1,2026-11-01,1000,10,200.0",
        "LOT-000002,L1,2026-11-01",                        # ligne 3 : coupée
        "LOT-000003,L2,2026-11-02,500,5,",
        "LOT-000004,L3",                                   # ligne 5 : coupée
        "LOT-000005,L1,2026-11-03,800,8,150.5",
    ]
    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return chemin


def _silver_du_fichier(contrat, chemin, registre=(), arrivee=ARRIVEE, contrat_du_silver=None, sans_coupees=False, deja_integrees=()):
    """Le parcours complet sur un vrai fichier : le registre décide, le bronze range, puis le silver rejuge le bronze."""
    _, ligne_registre = traiter_fichier(contrat, chemin, registre, arrivee)
    fichier = lire_selon_format(chemin, lire_nom_fichier(chemin.name).format)
    bronze = construire_lignes_bronze(contrat, ligne_registre, fichier)
    coupees = [] if sans_coupees else construire_lignes_coupees(ligne_registre, fichier)
    return ligne_registre, construire_silver(contrat_du_silver or contrat, ligne_registre, bronze, coupees, deja_integrees=deja_integrees)


def _typee(ligne):
    """La ligne du simulateur telle que le silver doit la rendre : entiers, décimal, date, absence."""
    return {
        "id_lot": ligne["id_lot"],
        "id_ligne": ligne["id_ligne"],
        "date_production": date.fromisoformat(ligne["date_production"]),
        "quantite_produite": int(ligne["quantite_produite"]),
        "quantite_rebut": int(ligne["quantite_rebut"]),
        "temperature_c": ligne["temperature_c"],
    }


# ==============================================================================
# 1. ✅ LES LIGNES VALIDES, TYPÉES, AVEC LEUR TRAÇABILITÉ
# ==============================================================================
def test_les_lignes_valides_sont_typees_et_gardent_leur_tracabilite(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert ligne_registre.statut == "ACCEPTEE"
    assert [ligne.valeurs for ligne in resultat.lignes] == [_typee(ligne) for ligne in generer_livraison(contrat, NB_LIGNES, GRAINE)]
    assert resultat.lignes[0] == LigneSilver(
        livraison_id=NOM, heure_arrivee=ARRIVEE, numero_ligne=1, version_contrat=contrat.version, valeurs=resultat.lignes[0].valeurs
    )
    assert [ligne.numero_ligne for ligne in resultat.lignes] == list(range(1, NB_LIGNES + 1))
    assert resultat.quarantaine == []


# ==============================================================================
# 2. 🚧 LA QUARANTAINE : les lignes rejetées d'une livraison acceptée avec rejets, lignes coupées comprises
# ==============================================================================
def test_les_lignes_rejetees_vont_en_quarantaine_avec_leurs_codes_et_leurs_colonnes(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})

    ligne_registre, resultat = _silver_du_fichier(contrat_sans_seuil, chemin)

    assert ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"
    assert len(resultat.lignes) == NB_LIGNES - 2
    lues = lire_csv(chemin).lignes
    assert [(q.numero_ligne, q.numero_fichier) for q in resultat.quarantaine] == [(19, None), (20, None)]
    assert [q.contenu for q in resultat.quarantaine] == lues[18:]                  # tels que reçus, en texte : on ne corrige rien
    assert {(v.code, v.colonnes) for q in resultat.quarantaine for v in q.violations} == {(CodeErreur.HORS_PLAGE, ("temperature_c",))}
    references = {(q.livraison_id, q.heure_arrivee, q.version_contrat) for q in resultat.quarantaine}
    assert references == {(NOM, ARRIVEE, contrat_sans_seuil.version)}


def test_les_lignes_coupees_vont_en_quarantaine_avec_leurs_champs_bruts(tmp_path, contrat_sans_seuil, entete):
    chemin = _ecrire_csv_avec_lignes_coupees(tmp_path, entete)

    ligne_registre, resultat = _silver_du_fichier(contrat_sans_seuil, chemin)

    assert ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"
    assert [ligne.numero_ligne for ligne in resultat.lignes] == [1, 2, 3]
    assert [(q.numero_fichier, q.numero_ligne, q.contenu) for q in resultat.quarantaine] == [
        (3, None, ("LOT-000002", "L1", "2026-11-01")),
        (5, None, ("LOT-000004", "L3")),
    ]
    assert {v.code for q in resultat.quarantaine for v in q.violations} == {CodeErreur.LIGNE_MALFORMEE}


def test_une_cle_en_conflit_va_en_quarantaine(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.CLE_EN_CONFLIT: 1})

    _, resultat = _silver_du_fichier(contrat_sans_seuil, chemin)

    assert len(resultat.lignes) == NB_LIGNES - 1
    assert [v.code for q in resultat.quarantaine for v in q.violations] == [CodeErreur.CLE_EN_CONFLIT]


def test_un_doublon_exact_est_ignore_sans_etre_rejete(tmp_path, contrat, entete):
    chemin = tmp_path / NOM
    ligne = "LOT-000001,L1,2026-11-01,1000,10,200.0"
    chemin.write_text("\n".join([",".join(entete), ligne, ligne, "LOT-000002,L2,2026-11-02,500,5,"]) + "\n", encoding="utf-8")

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert ligne_registre.statut == "ACCEPTEE"
    assert [(ligne.numero_ligne, ligne.valeurs["id_lot"]) for ligne in resultat.lignes] == [(1, "LOT-000001"), (3, "LOT-000002")]
    assert resultat.quarantaine == []                           # il reste au bronze : ni silver, ni quarantaine


# ==============================================================================
# 3. 🚫 CE QUE LE SILVER NE REND PAS : livraison bloquée, refusée ou rejouée
# ==============================================================================
def test_une_livraison_bloquee_ne_rend_rien(tmp_path, contrat, entete):
    chemin = _ecrire_csv_avec_lignes_coupees(tmp_path, entete)

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert ligne_registre.statut == "BLOQUEE"                  # 2 lignes coupées sur 5 : au-dessus du seuil
    assert resultat == RIEN


def test_une_livraison_refusee_ne_rend_rien(tmp_path, contrat, entete):
    chemin = tmp_path / NOM
    contenu = ",".join(entete[:-1]) + "\nLOT-000001,L1,2026-11-01,1000,10\nLOT-000002,L1\n"      # une colonne manque, une ligne est coupée
    chemin.write_text(contenu, encoding="utf-8")

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert (ligne_registre.statut, resultat) == ("REFUSEE", RIEN)


def test_un_rejeu_ne_rend_rien(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    premiere, premier_resultat = _silver_du_fichier(contrat, chemin)

    rejeu, resultat_du_rejeu = _silver_du_fichier(contrat, chemin, registre=[premiere])

    assert len(premier_resultat.lignes) == NB_LIGNES
    assert (rejeu.statut, resultat_du_rejeu) == ("REJEU_IGNORE", RIEN)


# ==============================================================================
# 4. ⚖️ LE SILVER DÉCIDE COMME LA RÉCEPTION (même version de contrat), SINON UNE ERREUR CLAIRE
# ==============================================================================
def _ecrire_livraison_a_trois_pour_cent(tmp_path, contrat):
    """100 lignes reçues : 98 bien formées dont 1 invalide, plus 2 lignes coupées. Réception : 3 rejets sur 100 (3 %, seuil 2 %) = BLOQUEE.
    Un silver qui ne verrait pas les lignes coupées compterait 1 rejet sur 98 (1 %) et intégrerait 97 lignes d'une livraison bloquée."""
    chemin = _ecrire(tmp_path, contrat, anomalies={CodeErreur.HORS_PLAGE: 1}, nb_lignes=98)
    with chemin.open("a", encoding="utf-8") as fichier:
        fichier.write("LOT-900001,L1\nLOT-900002,L2\n")
    return chemin


def test_le_silver_decide_comme_la_reception_meme_avec_des_lignes_coupees(tmp_path, contrat):
    chemin = _ecrire_livraison_a_trois_pour_cent(tmp_path, contrat)

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert (ligne_registre.statut, ligne_registre.nb_recues) == ("BLOQUEE", 100)
    assert resultat == RIEN


def test_un_silver_qui_ne_recoit_pas_les_lignes_coupees_est_refuse(tmp_path, contrat):
    """Le cas qui a motivé la conservation des lignes coupées au bronze : sans elles, 1 % de rejets au lieu de 3 %."""
    chemin = _ecrire_livraison_a_trois_pour_cent(tmp_path, contrat)

    with pytest.raises(ValueError, match="statut"):
        _silver_du_fichier(contrat, chemin, sans_coupees=True)


def test_un_statut_du_registre_different_de_celui_du_silver_donne_une_erreur_claire(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})
    _, ligne_registre = traiter_fichier(contrat_sans_seuil, chemin, (), ARRIVEE)
    fichier = lire_csv(chemin)
    bronze = construire_lignes_bronze(contrat_sans_seuil, ligne_registre, fichier)
    registre_mensonger = replace(ligne_registre, statut="ACCEPTEE")      # le registre dit « sans rejet », le contrat en trouve deux

    with pytest.raises(ValueError, match="ACCEPTEE_AVEC_REJETS"):
        construire_silver(contrat_sans_seuil, registre_mensonger, bronze, [])


def test_un_autre_contrat_est_un_retraitement_et_porte_sa_version(tmp_path, contrat, contrat_sans_seuil, entete):
    """Bloquée sous le contrat d'origine, la livraison peut être rejugée sous une autre version : le statut peut alors changer."""
    chemin = _ecrire_csv_avec_lignes_coupees(tmp_path, entete)
    _, ligne_registre = traiter_fichier(contrat, chemin, (), ARRIVEE)
    fichier = lire_csv(chemin)
    bronze, coupees = construire_lignes_bronze(contrat, ligne_registre, fichier), construire_lignes_coupees(ligne_registre, fichier)
    nouveau_contrat = replace(contrat_sans_seuil, version="2.1.0")

    resultat = construire_silver(nouveau_contrat, ligne_registre, bronze, coupees)

    assert ligne_registre.statut == "BLOQUEE"
    assert (len(resultat.lignes), len(resultat.quarantaine)) == (3, 2)
    assert {ligne.version_contrat for ligne in resultat.lignes} | {q.version_contrat for q in resultat.quarantaine} == {"2.1.0"}


# ==============================================================================
# 5. 🧱 LES MÊMES DÉCISIONS EN CSV ET EN PARQUET
# ==============================================================================
def _ecrire_parquet(tmp_path, lignes, nom=NOM_PARQUET):
    chemin = tmp_path / nom
    pq.write_table(pa.Table.from_pylist(lignes), chemin)
    return chemin


def test_un_parquet_donne_les_memes_decisions_et_les_memes_valeurs_qu_un_csv(tmp_path, contrat_sans_seuil):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, {CodeErreur.HORS_PLAGE: 2})
    typees = [dict(ligne, date_production=date.fromisoformat(ligne["date_production"])) for ligne in lignes]      # un vrai type date
    chemin_csv = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})
    chemin_parquet = _ecrire_parquet(tmp_path, typees)

    registre_csv, par_csv = _silver_du_fichier(contrat_sans_seuil, chemin_csv)
    registre_parquet, par_parquet = _silver_du_fichier(contrat_sans_seuil, chemin_parquet)

    assert registre_csv.statut == registre_parquet.statut == "ACCEPTEE_AVEC_REJETS"
    assert len(par_csv.lignes) > 0
    assert [ligne.valeurs for ligne in par_parquet.lignes] == [ligne.valeurs for ligne in par_csv.lignes]
    assert [q.numero_ligne for q in par_parquet.quarantaine] == [q.numero_ligne for q in par_csv.quarantaine]


def test_un_parquet_aux_dates_horodatees_est_bloque_aussi_au_silver(tmp_path, contrat):
    """Une date avec une heure est refusée partout (contrat 2.0.0) : la réception et le silver bloquent, sans écart ni erreur."""
    lignes = generer_livraison(contrat, 10, GRAINE)
    for ligne in lignes:
        ligne["date_production"] = datetime.fromisoformat(ligne["date_production"] + "T10:30:00")
    chemin = _ecrire_parquet(tmp_path, lignes)

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin)

    assert (ligne_registre.statut, resultat) == ("BLOQUEE", RIEN)


# ==============================================================================
# 6. 🧱 LA FORME DES LIGNES
# ==============================================================================
def test_les_lignes_du_silver_ne_se_modifient_pas(tmp_path, contrat):
    _, resultat = _silver_du_fichier(contrat, _ecrire(tmp_path, contrat))

    with pytest.raises(AttributeError):
        resultat.lignes[0].numero_ligne = 99
    with pytest.raises(AttributeError):
        resultat.lignes = []


def test_une_ligne_de_quarantaine_ne_se_modifie_pas():
    ligne = LigneQuarantaine(livraison_id=NOM, heure_arrivee=ARRIVEE, version_contrat="2.0.0", numero_ligne=1, numero_fichier=None,
                             contenu={}, violations=())

    with pytest.raises(AttributeError):
        ligne.numero_ligne = 99


# ==============================================================================
# 7. 🛡️ POINTS RELEVÉS À LA REVUE : un bronze incomplet, la clé interne des lignes coupées, la quarantaine partagée
# ==============================================================================
@pytest.mark.parametrize("version", [None, "9.9.9"], ids=["meme-version", "autre-version-retraitement"])
def test_un_bronze_incomplet_est_refuse_quelle_que_soit_la_version_du_contrat(tmp_path, contrat, version):
    """Une ligne valide manque au bronze : le statut recalculé reste « acceptée », seul le compte des lignes reçues le révèle.
    Sans cette garde, le silver perdrait la ligne en silence."""
    chemin = _ecrire(tmp_path, contrat)
    _, ligne_registre = traiter_fichier(contrat, chemin, (), ARRIVEE)
    fichier = lire_csv(chemin)
    bronze = construire_lignes_bronze(contrat, ligne_registre, fichier)
    contrat_du_silver = contrat if version is None else replace(contrat, version=version)

    with pytest.raises(ValueError, match="20 lignes"):
        construire_silver(contrat_du_silver, ligne_registre, bronze[:4] + bronze[5:], [])


def test_un_bronze_trop_long_est_refuse_aussi(tmp_path, contrat):
    """L'autre sens : une ligne en trop (ici un doublon exact, que le jugement ignorerait sans changer le statut)."""
    chemin = _ecrire(tmp_path, contrat)
    _, ligne_registre = traiter_fichier(contrat, chemin, (), ARRIVEE)
    bronze = construire_lignes_bronze(contrat, ligne_registre, lire_csv(chemin))

    with pytest.raises(ValueError, match="21 lignes"):
        construire_silver(contrat, ligne_registre, [*bronze, bronze[0]], [])


def test_un_contrat_avec_une_colonne_numero_ligne_ne_perturbe_pas_la_quarantaine(contrat_sans_seuil):
    """Les lignes coupées sont reconnues par leur identité, pas par une clé `numero_ligne` : un contrat peut avoir une colonne de ce nom."""
    colonne = replace(contrat_sans_seuil.colonnes[1], nom="numero_ligne")           # une copie de id_ligne : valeurs L1, L2 ou L3
    contrat = replace(contrat_sans_seuil, colonnes=(*contrat_sans_seuil.colonnes, colonne))
    base = {"id_ligne": "L1", "date_production": "2026-11-01", "quantite_produite": "1000", "quantite_rebut": "10",
            "temperature_c": "200.0"}
    bronze = [
        LigneBronze(NOM, ARRIVEE, 1, {"id_lot": "LOT-000001", **base, "numero_ligne": "L1"}),
        LigneBronze(NOM, ARRIVEE, 2, {"id_lot": "LOT-000002", **base, "numero_ligne": "L9"}),      # hors du domaine : rejetée
    ]
    ligne_registre = LigneRegistre(
        livraison_id=NOM, statut="ACCEPTEE_AVEC_REJETS", version_contrat=contrat.version, nb_recues=2, nb_valides=1, nb_rejetees=1,
        codes_refus=(), pays="FRA", usine="AA", ligne="001", horodatage_fichier=datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC),
        heure_arrivee=ARRIVEE, empreinte=None, rejeu_de=None,
    )

    resultat = construire_silver(contrat, ligne_registre, bronze, [])

    assert [ligne.numero_ligne for ligne in resultat.lignes] == [1]
    assert [(q.numero_ligne, q.numero_fichier) for q in resultat.quarantaine] == [(2, None)]
    assert [(v.code, v.colonnes) for v in resultat.quarantaine[0].violations] == [(CodeErreur.VALEUR_HORS_DOMAINE, ("numero_ligne",))]


def test_modifier_une_ligne_de_quarantaine_ne_modifie_pas_le_bronze(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 1})
    _, ligne_registre = traiter_fichier(contrat_sans_seuil, chemin, (), ARRIVEE)
    bronze = construire_lignes_bronze(contrat_sans_seuil, ligne_registre, lire_csv(chemin))
    avant = dict(bronze[-1].valeurs)

    resultat = construire_silver(contrat_sans_seuil, ligne_registre, bronze, [])
    resultat.quarantaine[0].contenu["temperature_c"] = "modifié par un consommateur"

    assert bronze[-1].valeurs == avant


# ==============================================================================
# 8. 🔁 LES DOUBLONS ENTRE LIVRAISONS : la première arrivée fait foi, sans jamais changer le statut décidé à la réception
# ==============================================================================
# Le silver reçoit les lignes silver des livraisons PLUS ANCIENNES (`deja_integrees`). Même clé et même contenu : ignorée, comme un doublon
# exact dans une livraison. Même clé et contenu différent : quarantaine CLE_EN_CONFLIT. Le statut et le seuil de rejet ne changent pas : la
# réception ne connaît que la livraison en cours, le silver ne doit jamais décider autrement qu'elle.
NOM_B = "livraison_production_FRA_AA_001_2026-11-12-09-00-00Z.csv"
ARRIVEE_B = ARRIVEE + timedelta(days=1)


def _livraison_a(tmp_path, contrat):
    """Une première livraison de 20 lignes, arrivée la première : ses lignes silver sont les « déjà intégrées » de la suivante."""
    _, resultat = _silver_du_fichier(contrat, _ecrire(tmp_path, contrat))
    return resultat.lignes


def _livraison_b(tmp_path, contrat, modifiees=0, nb_lignes=NB_LIGNES):
    """Une seconde livraison : les mêmes clés que la première, dont les `modifiees` premières lignes ont un contenu différent."""
    lignes = generer_livraison(contrat, nb_lignes, GRAINE)
    for ligne in lignes[:modifiees]:
        ligne["quantite_produite"] += 1
    chemin = tmp_path / NOM_B
    ecrire_livraison_csv(contrat, lignes, chemin)
    return chemin


def test_une_cle_deja_integree_avec_le_meme_contenu_est_ignoree(tmp_path, contrat):
    deja = _livraison_a(tmp_path, contrat)

    ligne_registre, resultat = _silver_du_fichier(contrat, _livraison_b(tmp_path, contrat), arrivee=ARRIVEE_B, deja_integrees=deja)

    assert (ligne_registre.statut, len(deja)) == ("ACCEPTEE", NB_LIGNES)
    assert resultat == RIEN                                    # les 20 lignes sont déjà là : ni silver, ni quarantaine


def test_une_cle_deja_integree_avec_un_autre_contenu_va_en_quarantaine_avec_la_livraison_d_origine(tmp_path, contrat):
    deja = _livraison_a(tmp_path, contrat)

    chemin = _livraison_b(tmp_path, contrat, modifiees=3, nb_lignes=22)

    ligne_registre, resultat = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B, deja_integrees=deja)

    assert ligne_registre.statut == "ACCEPTEE"                 # le statut décidé à la réception ne change pas
    assert [q.contenu for q in resultat.quarantaine] == lire_csv(chemin).lignes[:3]     # tel que reçu, en texte
    assert [ligne.numero_ligne for ligne in resultat.lignes] == [21, 22]            # les deux clés nouvelles entrent
    assert [q.numero_ligne for q in resultat.quarantaine] == [1, 2, 3]
    assert {(v.code, v.colonnes) for q in resultat.quarantaine for v in q.violations} == {(CodeErreur.CLE_EN_CONFLIT, contrat.cle_primaire)}
    assert all(NOM in v.message for q in resultat.quarantaine for v in q.violations)      # la livraison d'origine est nommée


def test_le_statut_ne_change_pas_meme_si_toutes_les_lignes_sont_en_conflit(tmp_path, contrat):
    """Si le conflit comptait dans le seuil, 20 rejets sur 20 bloqueraient la livraison : le silver déciderait autrement."""
    deja = _livraison_a(tmp_path, contrat)

    ligne_registre, resultat = _silver_du_fichier(
        contrat, _livraison_b(tmp_path, contrat, modifiees=NB_LIGNES), arrivee=ARRIVEE_B, deja_integrees=deja
    )

    assert ligne_registre.statut == "ACCEPTEE"
    assert (len(resultat.lignes), len(resultat.quarantaine)) == (0, NB_LIGNES)


def test_une_livraison_arrivee_plus_tard_n_est_pas_prise_en_compte(tmp_path, contrat):
    """La première arrivée fait foi : un retraitement ne voit que les livraisons arrivées AVANT lui."""
    plus_tard = [
        replace(ligne, livraison_id="plus_tard.csv", heure_arrivee=ARRIVEE_B + timedelta(days=1),
                valeurs={**ligne.valeurs, "quantite_produite": ligne.valeurs["quantite_produite"] + 1})
        for ligne in _livraison_a(tmp_path, contrat)
    ]

    _, resultat = _silver_du_fichier(contrat, _livraison_b(tmp_path, contrat), arrivee=ARRIVEE_B, deja_integrees=plus_tard)

    assert (len(resultat.lignes), resultat.quarantaine) == (NB_LIGNES, [])


def test_une_livraison_ne_se_compare_pas_a_elle_meme(tmp_path, contrat):
    chemin = _livraison_b(tmp_path, contrat)
    _, premier_passage = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B)

    _, second_passage = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B, deja_integrees=premier_passage.lignes)    # un retraitement

    assert second_passage == premier_passage and len(premier_passage.lignes) == NB_LIGNES


def test_une_ligne_invalide_ne_reserve_pas_sa_cle(tmp_path, contrat_sans_seuil):
    """La première livraison a rejeté la ligne 20 (hors plage) : elle n'est pas au silver, sa clé reste libre pour la livraison suivante."""
    _, premier = _silver_du_fichier(contrat_sans_seuil, _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 1}))
    assert len(premier.lignes) == NB_LIGNES - 1

    _, resultat = _silver_du_fichier(contrat_sans_seuil, _livraison_b(tmp_path, contrat_sans_seuil), arrivee=ARRIVEE_B,
                                     deja_integrees=premier.lignes)

    assert ([ligne.numero_ligne for ligne in resultat.lignes], resultat.quarantaine) == ([NB_LIGNES], [])


def test_sans_livraison_plus_ancienne_le_resultat_est_inchange(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)

    _, par_defaut = _silver_du_fichier(contrat, chemin)
    _, avec_liste_vide = _silver_du_fichier(contrat, chemin, deja_integrees=[])

    assert avec_liste_vide == par_defaut and len(par_defaut.lignes) == NB_LIGNES


def test_la_plus_ancienne_des_livraisons_anterieures_fait_foi(tmp_path, contrat):
    """Deux livraisons plus anciennes portent la même clé avec des contenus différents : c'est la PREMIÈRE arrivée qui fait foi."""
    premiere = _livraison_a(tmp_path, contrat)
    intermediaire = [
        replace(ligne, livraison_id="intermediaire.csv", heure_arrivee=ARRIVEE + timedelta(hours=12),
                valeurs={**ligne.valeurs, "quantite_produite": ligne.valeurs["quantite_produite"] + 1})
        for ligne in premiere
    ]
    chemin = _livraison_b(tmp_path, contrat, modifiees=NB_LIGNES)       # le même contenu que l'intermédiaire, différent de la première

    _, resultat = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B, deja_integrees=[*intermediaire, *premiere])

    assert (len(resultat.lignes), len(resultat.quarantaine)) == (0, NB_LIGNES)
    assert all(NOM in v.message and "intermediaire" not in v.message for q in resultat.quarantaine for v in q.violations)


def test_deux_livraisons_arrivees_a_la_meme_heure_se_departagent_par_leur_nom(tmp_path, contrat):
    """Sans départage, chacune ignorerait l'autre et le silver contiendrait deux lignes par clé : la première arrivée ne ferait plus foi."""
    chemin_a = _ecrire(tmp_path, contrat)                              # NOM, « plus petit » que NOM_B : elle passe en premier
    chemin_b = _livraison_b(tmp_path, contrat, modifiees=NB_LIGNES)
    _, a = _silver_du_fichier(contrat, chemin_a)
    _, b = _silver_du_fichier(contrat, chemin_b)                       # la MÊME heure d'arrivée : ARRIVEE

    _, b_apres_a = _silver_du_fichier(contrat, chemin_b, deja_integrees=a.lignes)
    _, a_apres_b = _silver_du_fichier(contrat, chemin_a, deja_integrees=b.lignes)

    assert (len(b_apres_a.lignes), len(b_apres_a.quarantaine)) == (0, NB_LIGNES)        # B voit A : conflit
    assert (len(a_apres_b.lignes), len(a_apres_b.quarantaine)) == (NB_LIGNES, 0)        # A ne voit pas B : elle fait foi


def test_une_livraison_retraitee_plus_tard_ne_se_compare_pas_a_ses_propres_lignes(tmp_path, contrat):
    """La livraison courante est exclue par son NOM, pas par l'égalité d'heure : retraitée plus tard, elle rend les mêmes lignes."""
    chemin = _livraison_b(tmp_path, contrat)
    _, premier_passage = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B)

    plus_tard = ARRIVEE_B + timedelta(hours=1)
    _, retraitement = _silver_du_fichier(contrat, chemin, arrivee=plus_tard, deja_integrees=premier_passage.lignes)

    assert len(premier_passage.lignes) == NB_LIGNES
    assert [ligne.valeurs for ligne in retraitement.lignes] == [ligne.valeurs for ligne in premier_passage.lignes]
    assert retraitement.quarantaine == []


@pytest.mark.parametrize("a_l_envers", [False, True], ids=["ordre-direct", "ordre-inverse"])
def test_le_departage_des_plus_anciennes_ne_depend_pas_de_l_ordre_des_lignes_passees(tmp_path, contrat, a_l_envers):
    """Deux livraisons plus anciennes arrivées à la même heure portent la même clé : le NOM décide laquelle fait foi, pas l'ordre."""
    base = _livraison_a(tmp_path, contrat)
    premiere = [replace(ligne, livraison_id="a.csv") for ligne in base]
    seconde = [replace(ligne, livraison_id="b.csv", valeurs={**ligne.valeurs, "quantite_produite": ligne.valeurs["quantite_produite"] + 1})
               for ligne in base]
    deja = [*seconde, *premiere] if a_l_envers else [*premiere, *seconde]
    chemin = _livraison_b(tmp_path, contrat, modifiees=NB_LIGNES)       # le même contenu que « b.csv », différent de « a.csv »

    _, resultat = _silver_du_fichier(contrat, chemin, arrivee=ARRIVEE_B, deja_integrees=deja)

    assert (len(resultat.lignes), len(resultat.quarantaine)) == (0, NB_LIGNES)
    assert all("a.csv" in v.message and "b.csv" not in v.message for q in resultat.quarantaine for v in q.violations)
