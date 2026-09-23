"""Canonical Pin-count configuration shared by manual and automated workflows.
The production workflow is fixed at four Pins per product; legacy environment
overrides cannot re-enable the retired five-Pin path.
"""
from __future__ import annotations

PINS_PER_PRODUCT = 4
MAX_PINS_PER_PRODUCT = 4
