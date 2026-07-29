# -*- coding: utf-8 -*-
"""Backward-compatible alias for config.prod_prompt (tmp-prod deploy / old imports)."""
from __future__ import annotations

from config.prod_prompt import *  # noqa: F403
from config.prod_prompt import (  # noqa: F401
    build_prod_compliance_opening as build_tmp_prod_compliance_opening,
    build_prod_opening as build_tmp_prod_opening,
    build_prod_opening_parts as build_tmp_prod_opening_parts,
    build_prod_system_role as build_tmp_prod_system_role,
    prod_slots_from_payload as tmp_prod_slots_from_payload,
)
