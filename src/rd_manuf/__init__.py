"""rd_manuf — logique métier pure (aucune dépendance à Spark ni à Databricks).

Pourquoi « pure » ?
    Ce code se teste en quelques secondes sur n'importe quelle machine (et dans GitHub Actions),
    sans cluster. Les notebooks ne feront que le brancher aux tables et aux Volumes.
"""
