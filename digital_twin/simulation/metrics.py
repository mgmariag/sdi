"""Experiment output aggregation helpers.

Daily and hourly metrics are currently produced inside ``engine.py`` because
those aggregations share in-memory simulation state. This module marks the
metrics boundary for the next low-risk extraction.
"""
