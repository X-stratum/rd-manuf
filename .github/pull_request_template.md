## En clair
<!-- 2 ou 3 phrases SANS jargon : ce que ça permet, vu du métier. Un lecteur qui ne connaît pas le code doit comprendre l'intérêt. -->

## Pourquoi
<!-- Le problème ou le besoin, en 1-2 phrases. Pas le « comment » : le diff le montre. -->

## Ce qui change
<!-- Liste courte. Mentionner explicitement tout changement de contrat de données (version) ou de garde-fou. -->

## Décisions à retenir
<!-- Les choix à ne pas perdre : règle métier tranchée, compromis, ce qu'on a écarté. Écrire « aucune » si rien. -->

## Impact données
- [ ] Aucun changement de schéma ni de règle de contrat
- [ ] Contrat modifié → version incrémentée (patch / mineure / majeure) : `…`
- [ ] Tables concernées : `…` (bronze / silver / gold / ops)

## Preuves
- [ ] Test unitaire ajouté ou adapté pour chaque règle modifiée
- [ ] Scénario de simulation ajouté si un nouveau cas d'erreur est géré
- [ ] CI verte
<!-- Ici : rouge vu avant le code, sabotage et résultat, nombre de tests. Cela reste dans la PR et n'entre pas dans le message de squash. -->

## Risques et retour arrière
<!-- Que se passe-t-il si c'est faux en prod ? Comment revenir en arrière ? -->

## Auto-revue (en solo, GitHub interdit d'approuver sa propre PR)
- [ ] J'ai relu mon diff en entier comme si c'était celui de quelqu'un d'autre
- [ ] Aucun secret, jeton, chemin personnel ni nom de catalogue en dur ajouté
- [ ] Revue Claude demandée (`/code-review`) et les points retenus sont notés ci-dessous

## Message de squash
<!--
À copier dans le formulaire « Squash and merge » : le titre dans le champ à une ligne, le corps dans la grande zone.
Ce texte devient l'historique de `main` : il doit se comprendre sans ouvrir la PR, sans case à cocher ni chiffre de preuve.
À rédiger quand la PR est prête, et à relire avant de confirmer le merge.
-->

**Titre**
```
type(portée): ce que ça change, au présent
```

**Corps**
```
En clair : <ce que ça permet, vu du métier, en 2 ou 3 phrases>

Pourquoi : <le problème ou le besoin>

Ce qui change :
- <point 1>
- <point 2>

Décisions : <les choix à retenir, ou « aucune »>
```
