# -*- coding: utf-8 -*-
"""豆包（Doubao）外呼音色目录：prod / PM 选型用。"""
from __future__ import annotations

from typing import Any

from backend.duplex_client import OUTBOUND_SPEAKER

# 场景 → 音色列表（name / voice_type / lang）
DOUBAO_VOICE_CATALOG: list[dict[str, Any]] = [
    {
        "scene": "S2S-Omni",
        "scene_id": "s2s_omni",
        "voices": [
            {
                "name": "vivi",
                "voice_type": "zh_female_vv_jupiter_bigtts",
                "lang": "中文",
            },
            {
                "name": "小何",
                "voice_type": "zh_female_xiaohe_jupiter_bigtts",
                "lang": "中文",
            },
            {
                "name": "云舟",
                "voice_type": "zh_male_yunzhou_jupiter_bigtts",
                "lang": "中文",
            },
            {
                "name": "小天",
                "voice_type": "zh_male_xiaotian_jupiter_bigtts",
                "lang": "中文",
            },
        ],
    },
    {
        "scene": "SC 2.0版本",
        "scene_id": "sc_2_0",
        "voices": [
            {
                "name": "傲娇女友",
                "voice_type": "saturn_zh_female_aojiaonvyou_tob",
                "lang": "中文",
            },
            {
                "name": "病娇姐姐",
                "voice_type": "saturn_zh_female_bingjiaojiejie_tob",
                "lang": "中文",
            },
            {
                "name": "成熟姐姐",
                "voice_type": "saturn_zh_female_chengshujiejie_tob",
                "lang": "中文",
            },
            {
                "name": "可爱女生",
                "voice_type": "saturn_zh_female_keainvsheng_tob",
                "lang": "中文",
            },
            {
                "name": "暖心学姐",
                "voice_type": "saturn_zh_female_nuanxinxuejie_tob",
                "lang": "中文",
            },
            {
                "name": "贴心女友",
                "voice_type": "saturn_zh_female_tiexinnvyou_tob",
                "lang": "中文",
            },
            {
                "name": "温柔文雅",
                "voice_type": "saturn_zh_female_wenrouwenya_tob",
                "lang": "中文",
            },
            {
                "name": "妩媚御姐",
                "voice_type": "saturn_zh_female_wumeiyujie_tob",
                "lang": "中文",
            },
            {
                "name": "性感御姐",
                "voice_type": "saturn_zh_female_xingganyujie_tob",
                "lang": "中文",
            },
            {
                "name": "傲气凌人",
                "voice_type": "saturn_zh_male_aiqilingren_tob",
                "lang": "中文",
            },
            {
                "name": "傲娇公子",
                "voice_type": "saturn_zh_male_aojiaogongzi_tob",
                "lang": "中文",
            },
            {
                "name": "傲娇精英",
                "voice_type": "saturn_zh_male_aojiaojingying_tob",
                "lang": "中文",
            },
            {
                "name": "傲慢少爷",
                "voice_type": "saturn_zh_male_aomanshaoye_tob",
                "lang": "中文",
            },
            {
                "name": "霸道少爷",
                "voice_type": "saturn_zh_male_badaoshaoye_tob",
                "lang": "中文",
            },
            {
                "name": "病娇白莲",
                "voice_type": "saturn_zh_male_bingjiaobailian_tob",
                "lang": "中文",
            },
            {
                "name": "不羁青年",
                "voice_type": "saturn_zh_male_bujiqingnian_tob",
                "lang": "中文",
            },
            {
                "name": "成熟总裁",
                "voice_type": "saturn_zh_male_chengshuzongcai_tob",
                "lang": "中文",
            },
            {
                "name": "磁性男嗓",
                "voice_type": "saturn_zh_male_cixingnansang_tob",
                "lang": "中文",
            },
            {
                "name": "醋精男友",
                "voice_type": "saturn_zh_male_cujingnanyou_tob",
                "lang": "中文",
            },
            {
                "name": "风发少年",
                "voice_type": "saturn_zh_male_fengfashaonian_tob",
                "lang": "中文",
            },
            {
                "name": "腹黑公子",
                "voice_type": "saturn_zh_male_fuheigongzi_tob",
                "lang": "中文",
            },
        ],
    },
    {
        "scene": "O 2.0",
        "scene_id": "o_2_0",
        "voices": [
            {
                "name": "Tim",
                "voice_type": "en_male_tim_uranus_bigtts",
                "lang": "美式英语",
            },
            {
                "name": "Dacey",
                "voice_type": "en_female_dacey_uranus_bigtts",
                "lang": "美式英语",
            },
            {
                "name": "Stokie",
                "voice_type": "en_female_stokie_uranus_bigtts",
                "lang": "美式英语",
            },
        ],
    },
]

DEFAULT_OUTBOUND_VOICE = OUTBOUND_SPEAKER


def default_outbound_voice() -> str:
    """Runtime default: config.json outbound.sip.voice (env override) → hardcoded fallback."""
    try:
        from backend.sip.config import resolve_outbound_voice

        return resolve_outbound_voice() or DEFAULT_OUTBOUND_VOICE
    except Exception:
        return DEFAULT_OUTBOUND_VOICE


def _index() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for group in DOUBAO_VOICE_CATALOG:
        scene = str(group.get("scene") or "")
        scene_id = str(group.get("scene_id") or "")
        for v in group.get("voices") or []:
            vt = str(v.get("voice_type") or "").strip()
            if not vt:
                continue
            out[vt] = {
                "name": str(v.get("name") or vt),
                "voice_type": vt,
                "lang": str(v.get("lang") or ""),
                "scene": scene,
                "scene_id": scene_id,
            }
    return out


VOICE_BY_TYPE = _index()
ALLOWED_VOICE_TYPES = frozenset(VOICE_BY_TYPE.keys())


def list_voice_catalog(*, default_voice: str = "") -> dict[str, Any]:
    default = (default_voice or default_outbound_voice()).strip() or DEFAULT_OUTBOUND_VOICE
    return {
        "default_voice": default,
        "default_meta": VOICE_BY_TYPE.get(default),
        "scenes": DOUBAO_VOICE_CATALOG,
        "count": len(ALLOWED_VOICE_TYPES),
    }


def resolve_voice(voice: str | None, *, default: str = "") -> str:
    """空 → 默认音色；非空必须在白名单内，否则 ValueError。"""
    raw = (voice or "").strip()
    fallback = (default or default_outbound_voice()).strip() or DEFAULT_OUTBOUND_VOICE
    if not raw:
        return fallback
    if raw not in ALLOWED_VOICE_TYPES:
        raise ValueError(
            f"Unsupported voice_type: {raw}. "
            f"Use GET /prod/voice-catalog 或 /outbound/v1/voices 查看可用音色。"
        )
    return raw


def voice_meta(voice_type: str) -> dict[str, str] | None:
    return VOICE_BY_TYPE.get((voice_type or "").strip())
