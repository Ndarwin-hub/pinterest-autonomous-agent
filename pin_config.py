"""Canonical Pin-count configuration shared by manual and automated workflows."""
from __future__ import annotations
import os

PINS_PER_PRODUCT = max(1, min(int(os.getenv("PINS_PER_PRODUCT", "4") or "4"), 10))
MAX_PINS_PER_PRODUCT = PINS_PER_PRODUCT
