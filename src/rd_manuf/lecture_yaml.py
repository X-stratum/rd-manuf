"""Lecture des fichiers YAML du dépôt (contrat, catalogue d'alertes, surveillance) : une clé répétée est refusée."""
from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml


class _LecteurSansCleRepetee(yaml.SafeLoader):
    """PyYAML garde en silence la DERNIÈRE valeur d'une clé répétée : une copie-colle changerait une version, un seuil ou le rôle qui
    doit agir, sans un mot. Ce lecteur refuse la répétition, avec le nom de la clé et la ligne du fichier."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        vues: set[Any] = set()
        for noeud_cle, _ in node.value:
            if noeud_cle.tag == "tag:yaml.org,2002:merge":                 # la fusion `<<` a sa propre règle : ses clés se remplacent
                continue
            cle = self.construct_object(noeud_cle, deep=True)
            if isinstance(cle, Hashable):
                if cle in vues:
                    raise yaml.constructor.ConstructorError(None, None, f"la clé « {cle} » est répétée", noeud_cle.start_mark)
                vues.add(cle)
        return super().construct_mapping(node, deep)


def lire_yaml_sans_cle_repetee(chemin: str | Path) -> Any:
    """Lit le fichier. Une clé répétée, à n'importe quelle profondeur, lève une `yaml.YAMLError` ; l'appelant la traduit en son erreur."""
    return yaml.load(Path(chemin).read_text(encoding="utf-8"), Loader=_LecteurSansCleRepetee)
