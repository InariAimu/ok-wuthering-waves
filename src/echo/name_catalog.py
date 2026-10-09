"""Canonical Simplified-Chinese Echo names used to constrain OCR.

Source: https://wiki.kurobbs.com/mc/catalogue/list?fid=1099&sid=1107
Snapshot: 2026-08-27 (199 entries)
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


ECHO_NAME_CATALOG_SOURCE = (
    "https://wiki.kurobbs.com/mc/catalogue/list?fid=1099&sid=1107"
)
ECHO_NAME_CATALOG_SNAPSHOT = "2026-08-27"

ECHO_NAMES_ZH_CN = (
    "天傀劫煞", "融躯战士", "万囮牢·朽躯", "霁息兽尊", "不熄猎手",
    "霁息兽尊·首", "霁息兽尊·身", "心傀·恐", "心傀·悲", "心傀·思",
    "心傀·忧", "心傀·怒", "心傀·喜", "金庭候", "石庭候", "瓷庭候",
    "封庭械囿", "千傀重楼", "共鸣回响·梦魇亚当·重锤", "共鸣回响·达妮娅",
    "共鸣回响·鸣式·虚造神型", "辛吉勒姆", "无铭探索者", "海维夏",
    "炉芯机骸", "迷胧幻蛾", "格洛犸图", "共鸣回响·冠顶苍隼", "冠顶械隼",
    "风鳞蜃甲", "锯袭铁影", "隐迹铁影", "霜鳞蜃甲", "重工铁蹄",
    "探隧重机", "双极·渊陨重锋", "双极·星升辉铳", "矿岩机麋", "莳植机麋",
    "冰盈舞者", "噼啪啪", "岩蛛S4型", "矿岩熊蜂", "影烁者", "莳植熊蜂",
    "颤栗战士", "共鸣回响·鸣式·利维亚坦", "梦魇·赫卡忒", "共鸣回响·芬莱克",
    "赫卡忒", "角", "鸣钟之龟", "梦魇·凯尔匹", "梦魇·辉萤军势",
    "梦魇·哀声鸷", "梦魇·燎照之骑", "梦魇·无冠者", "梦魇·朔雷之鳞",
    "梦魇·云闪之鳞", "梦魇·无常凶鹭", "梦魇·飞廉之猩", "海之女",
    "伪作的神王", "共鸣回响·芙露德莉斯", "无妄者", "荣耀狮像", "叹息古龙",
    "异构武装", "罗蕾莱", "无归的谬误", "辉萤军势", "燎照之骑", "哀声鸷",
    "无冠者", "无常凶鹭", "飞廉之猩", "聚械机偶", "云闪之鳞", "朔雷之鳞",
    "梦魇·刺玫菇", "梦魇·绿熔蜥", "蚀脊龙", "梦魇·青羽鹭", "梦魇·紫羽鹭",
    "梦魇·振铎乐师", "角鳄", "传道者的遗形", "重塑雕像的拳砾", "飓力熊",
    "巨布偶", "荣光节使", "浮灵偶", "凝水贵族", "持刃贵族", "毒冠贵族",
    "暗夜骑士", "幻昼骑士", "琉璃刀伶", "巡游骑士", "游鳞机枢", "雪鬃狼",
    "巡哨机傀", "绿熔蜥", "踏光兽", "磐石守卫", "振铎乐师", "暗鬃狼",
    "刺玫菇", "青羽鹭", "戏猿", "车刃镰", "奏谕乐师", "紫羽鹭",
    "箭簇熊", "坚岩斗士", "冥渊守卫", "梦魇·呜咔咔", "梦魇·侏侏鸵",
    "梦魇·刺玫菇（稚形）", "梦魇·绿熔蜥（稚形）", "梦魇·惊蛰猎手",
    "梦魇·巡徊猎手", "梦魇·啾啾河豚", "梦魇·咕咕河豚", "梦魇·审判战士",
    "梦魇·破霜猎手", "小翼龙·衍射", "小翼龙·热熔", "小翼龙·湮灭",
    "苦信者的作俑", "慈悲节使", "赦罪节使", "卫冕节使", "小翼龙·气动",
    "小翼龙·导电", "小翼龙·冷凝", "气动棱镜", "愚金幼岩", "釉变幼岩",
    "霜鬃狼", "雷鬃狼", "风鬃狼", "欺诈奇藏", "工头布偶", "寂寞小姐",
    "魔术先生", "云海妖精", "幽翎火", "浮灵偶·莱特", "浮灵偶·蕾弗",
    "浮灵偶·海德", "叮咚咚", "破霜猎手", "寒霜陆龟", "咕咕河豚",
    "冷凝棱镜", "热熔棱镜", "绿熔蜥（稚形）", "鸣泣战士", "融火虫",
    "火鬃狼", "咔嚓嚓", "阿嗞嗞", "通行灯偶", "衍射棱镜", "游弋蝶",
    "审判战士", "呜咔咔", "湮灭棱镜", "刺玫菇（稚形）", "呼咻咻",
    "啾啾河豚", "巡徊猎手", "幼猿", "惊蛰猎手", "晶螯蝎", "遁地鼠",
    "先锋幼岩", "裂变幼岩", "碎獠猪", "侏侏鸵", "孪面", "云海列车",
    "留声奏者", "祖渊海触", "西莫斯先生", "贡多拉", "卫冕节使（公）",
    "章鱼先生", "拉里奥", "浴缸犬", "焙焙", "高天讯使", "弄臣",
    "墨丘利机", "颂诏节使", "流明灯使", "哈德良角斗士的肖像", "饰音歌姬",
)


_PUNCTUATION_TRANSLATION = str.maketrans({
    "•": "·", "・": "·", "∙": "·", "．": "·", ".": "·",
    "(": "（", ")": "）",
})


def normalize_echo_name_text(value: str) -> str:
    """Normalize OCR spacing, width and common middle-dot variants."""

    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.translate(_PUNCTUATION_TRANSLATION)
    return re.sub(r"\s+", "", normalized).strip()


@dataclass(frozen=True)
class EchoNameMatch:
    raw: str
    canonical: str
    method: str
    similarity: float
    alternatives: tuple[str, ...] = ()


class EchoNameRecognitionError(ValueError):
    def __init__(self, raw: str, reason: str, alternatives: tuple[str, ...] = ()):
        self.raw = raw
        self.reason = reason
        self.alternatives = alternatives
        suffix = f"; candidates={alternatives}" if alternatives else ""
        super().__init__(f"Echo name OCR {raw!r}: {reason}{suffix}")


def match_echo_name(
        raw: str, *, catalog: tuple[str, ...] = ECHO_NAMES_ZH_CN,
        ambiguity_margin: float = 0.08) -> EchoNameMatch:
    """Resolve OCR text against the legal catalogue without silent guessing."""

    value = normalize_echo_name_text(raw)
    if not value:
        raise EchoNameRecognitionError(raw, "empty after normalization")
    normalized_catalog = tuple(
        (name, normalize_echo_name_text(name)) for name in catalog
    )
    exact = tuple(name for name, key in normalized_catalog if key == value)
    if len(exact) == 1:
        return EchoNameMatch(raw, exact[0], "exact", 1.0)

    prefix = tuple(
        name for name, key in normalized_catalog
        if len(value) >= 2 and key.startswith(value)
    )
    if len(prefix) == 1:
        canonical = prefix[0]
        similarity = len(value) / len(normalize_echo_name_text(canonical))
        return EchoNameMatch(raw, canonical, "unique_prefix", similarity)
    if len(prefix) > 1:
        raise EchoNameRecognitionError(raw, "ambiguous prefix", prefix[:5])

    scored = sorted((
        (SequenceMatcher(None, value, key).ratio(), name)
        for name, key in normalized_catalog
    ), reverse=True)
    best_score, best_name = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    minimum = 1.0 if len(value) <= 2 else (0.75 if len(value) <= 4 else 0.78)
    alternatives = tuple(name for score, name in scored[:5] if score >= best_score - 0.08)
    if best_score < minimum:
        raise EchoNameRecognitionError(raw, "similarity below threshold", alternatives)
    if best_score - second_score < ambiguity_margin:
        raise EchoNameRecognitionError(raw, "fuzzy match is ambiguous", alternatives)
    return EchoNameMatch(raw, best_name, "fuzzy", best_score, alternatives)

