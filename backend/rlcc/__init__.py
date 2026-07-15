# -*- coding: utf-8 -*-
"""容联云 RLCC 呼叫中心接入模块。"""

from backend.rlcc.config import load_rlcc_config
from backend.rlcc.client import RlccClient

__all__ = ["load_rlcc_config", "RlccClient"]
