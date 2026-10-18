"""名称归一化与「疑似重复」判定（CONTRACTS §5 /《方案》第五部分 44 项）。

设计约束（不可违反）：

* **纯函数**：不读写文件、不访问网络、不使用全局可变状态、不依赖当前时间。
* **幂等**：``normalize(normalize(x)) == normalize(x)``。
  实现方式是把归一化写成「重复施加同一步函数直到不动点」，不动点天然幂等。
* **确定性**：同一输入永远得到同一输出，与运行环境、随机种子无关。
  这是 R1 落地的前提——DeepSeek 种子不固定，同一素材名每次输出可能不同，
  归一化不一致就会造成「该复用的被当新增重新生成 / 不该覆盖的被覆盖」。

归一化步骤（按顺序、循环到不动点）：

1. Unicode NFKC：全角 ASCII（Ａ→A、１→1）、全角标点（，→, ！→!）归一。
2. ``casefold()``：统一小写（比 ``lower()`` 更彻底，例如 ß→ss）。
3. 标点统一为半角/ASCII 规范形式（中英标点统一）：。→. 、→, 【】→[] 「」『』“”→" 等。
4. 删除所有空白字符：首尾空格、内部空格、Tab、换行、U+3000 全角空格、
   U+00A0 不换行空格，以及零宽字符（U+200B/200C/200D/2060/FEFF）——
   零宽字符是模型输出里最常见的「看起来一模一样但字符串不等」的来源。

注意第 4 步会删除**内部空格**（"Iron Man" → "ironman"），这是《方案》
「去空格」的明确要求；但也因此，仅差空格的名称属于**精确匹配**而不是疑似重复。
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

__all__ = [
    "normalize_name",
    "is_suspect",
    "similarity",
    "variant_key",
    "PUNCTUATION_MAP",
]


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

#: 不动点迭代上限。正常情况下 2~3 轮即收敛；上限只是防御性护栏。
_MAX_PASSES = 8

#: 需要显式删除的零宽 / 不可见字符（``str.isspace()`` 不覆盖它们）。
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff\u180e\u00ad")

#: 标点统一表：键 -> ASCII 规范形式。
#: 多数全角标点在 NFKC 阶段已转成 ASCII，这里保留一份「保底 + 中文标点」映射。
PUNCTUATION_MAP: dict[str, str] = {
    "\u3002": ".",    # 。
    "\u3001": ",",    # 、
    "\uff0c": ",",    # ，
    "\uff1b": ";",    # ；
    "\uff1a": ":",    # ：
    "\uff01": "!",    # ！
    "\uff1f": "?",    # ？
    "\uff08": "(",    # （
    "\uff09": ")",    # ）
    "\u3010": "[",    # 【
    "\u3011": "]",    # 】
    "\u300c": '"',    # 「
    "\u300d": '"',    # 」
    "\u300e": '"',    # 『
    "\u300f": '"',    # 』
    "\u300a": "<",    # 《
    "\u300b": ">",    # 》
    "\u3008": "<",    # 〈
    "\u3009": ">",    # 〉
    "\u2014": "-",    # —
    "\u2013": "-",    # –
    "\u2015": "-",    # ―
    "\ufe63": "-",    # ﹣
    "\uff0d": "-",    # －
    "\uff5e": "~",    # ～
    "\u301c": "~",    # 〜
    "\u30fb": ".",    # ・
    "\u00b7": ".",    # ·
    "\u2027": ".",    # ‧
    "\u2026": "...",  # …
    "\u2025": "...",  # ‥
    "\uff0e": ".",    # ．
    "\uff3f": "_",    # ＿
    "\uff0a": "*",    # ＊
    "\uff03": "#",    # ＃
    "\uff20": "@",    # ＠
    "\uff5c": "|",    # ｜
    "\uff0f": "/",    # ／
    "\uff3c": "\\",   # ＼
    "\uff3b": "[",    # ［
    "\uff3d": "]",    # ］
    "\uff5b": "{",    # ｛
    "\uff5d": "}",    # ｝
    "\u2018": "'",    # ‘
    "\u2019": "'",    # ’
    "\u201c": '"',    # “
    "\u201d": '"',    # ”
}


def _step(text: str) -> str:
    """归一化的单步变换（内部函数，不承诺幂等——由调用方迭代到不动点）。"""
    out = unicodedata.normalize("NFKC", text)
    out = out.casefold()
    if any(ch in PUNCTUATION_MAP for ch in out):
        out = "".join(PUNCTUATION_MAP.get(ch, ch) for ch in out)
    if any(ch.isspace() or ch in _ZERO_WIDTH for ch in out):
        out = "".join(ch for ch in out if not (ch.isspace() or ch in _ZERO_WIDTH))
    return out


def normalize_name(name: str) -> str:
    """把素材名归一化为稳定比较键。纯函数、幂等、确定性。

    >>> normalize_name("  林 澈 ")
    '林澈'
    >>> normalize_name("Iron  Man")
    'ironman'
    >>> normalize_name("「钢铁侠」")
    '"钢铁侠"'
    """
    if not isinstance(name, str):
        raise TypeError(f"normalize_name 只接受 str，收到 {type(name).__name__}：{name!r}")
    if not name:
        return ""
    text = name
    for _ in range(_MAX_PASSES):
        nxt = _step(text)
        if nxt == text:
            return text
        text = nxt
    return text


# ---------------------------------------------------------------------------
# 疑似重复判定
# ---------------------------------------------------------------------------

#: 序数词标记：仅在「疑似判定」中剥离，归一化本身不剥离。
_ORDINAL_MARKERS = frozenset("第号號位个個只隻")

#: 中文数字 -> 阿拉伯数字（启发式，仅用于疑似判定）。
_CN_DIGITS: dict[str, str] = {
    "零": "0", "〇": "0", "○": "0",
    "一": "1", "壹": "1",
    "二": "2", "两": "2", "兩": "2", "贰": "2", "貳": "2",
    "三": "3", "叁": "3",
    "四": "4", "肆": "4",
    "五": "5", "伍": "5",
    "六": "6", "陆": "6", "陸": "6",
    "七": "7", "柒": "7",
    "八": "8", "捌": "8",
    "九": "9", "玖": "9",
    "十": "10", "拾": "10",
}

#: 常见繁体 -> 简体字表（有限集合，只用于疑似判定，不参与 normalize_name）。
#: 局限：未收录的繁简差异只能靠相似度阈值兜住，见 ``is_suspect``。
_T2S_PAIRS: tuple[str, ...] = (
    "鐵铁", "劍剑", "龍龙", "俠侠", "車车", "東东", "馬马", "鳥鸟", "魚鱼",
    "語语", "說说", "話话", "這这", "們们", "個个", "來来", "時时", "會会",
    "後后", "裡里", "髮发", "頭头", "臉脸", "體体", "醫医", "學学", "國国",
    "圖图", "畫画", "幾几", "麼么", "樣样", "對对", "錯错", "兒儿", "貓猫",
    "陽阳", "陰阴", "風风", "雲云", "電电", "靈灵", "戰战", "鬥斗", "軍军",
    "師师", "將将", "場场", "樹树", "葉叶", "蟲虫", "獸兽", "裝装", "飾饰",
    "傘伞", "燈灯", "鏡镜", "錶表", "鐘钟", "銀银", "鋼钢", "飛飞", "機机",
    "門门", "牆墙", "橋桥", "綠绿", "藍蓝", "紅红", "黃黄", "無无", "萬万",
    "與与", "為为", "於于", "產产", "費费", "貴贵", "買买", "賣卖", "讀读",
    "寫写", "聽听", "見见", "觀观", "覺觉", "認认", "識识", "達达", "遠远",
    "邊边", "進进", "過过", "還还", "運运", "選选", "餓饿", "飯饭", "飲饮",
    "藥药", "廠厂", "廣广", "應应", "聲声", "術术", "衝冲", "擊击", "據据",
    "權权", "檔档", "資资", "線线", "組组", "統统", "計计", "總总", "級级",
    "約约", "給给", "結结", "練练", "續续", "絕绝", "紀纪", "紙纸", "緊紧",
    "編编", "緣缘", "職职", "聯联", "腦脑", "舉举", "舊旧", "蘭兰", "藝艺",
    "處处", "號号", "雖虽", "隨随", "難难", "靜静", "響响", "預预", "願愿",
    "類类", "養养", "驗验", "點点", "齊齐", "淚泪", "憶忆", "億亿", "倉仓",
    "儀仪", "價价", "儉俭", "傑杰", "內内", "冊册", "凍冻", "凱凯", "劉刘",
    "劍剑", "勳勋", "區区", "華华", "協协", "單单", "嚴严", "喪丧", "嚮向",
    "團团", "園园", "壞坏", "壘垒", "夠够", "夢梦", "奧奥", "婦妇", "媽妈",
    "娛娱", "孫孙", "寶宝", "尋寻", "島岛", "嶺岭", "帥帅", "帶带", "幫帮",
    "廣广", "廳厅", "張张", "徹彻", "態态", "懷怀", "懸悬", "慶庆", "憲宪",
    "戶户", "掃扫", "揚扬", "換换", "損损", "搖摇", "搶抢", "撲扑", "擇择",
    "擔担", "據据", "擴扩", "攝摄", "敗败", "敵敌", "數数", "斷断", "曆历",
    "書书", "術术", "榮荣", "樹树", "檢检", "樓楼", "歐欧", "歸归", "殺杀",
    "氣气", "決决", "沒没", "沖冲", "溝沟", "溫温", "滅灭", "滿满", "潔洁",
    "瀏浏", "燈灯", "營营", "爾尔", "牆墙", "獨独", "獲获", "現现", "環环",
    "畢毕", "異异", "當当", "疇畴", "療疗", "發发", "監监", "盡尽", "眾众",
    "睜睁", "矯矫", "確确", "禮礼", "種种", "積积", "稱称", "穩稳", "窮穷",
    "競竞", "筆笔", "節节", "範范", "築筑", "簡简", "粵粤", "精精", "糧粮",
    "紅红", "約约", "純纯", "紙纸", "素素", "緊紧", "縣县", "總总", "織织",
    "繩绳", "羅罗", "翹翘", "聯联", "聲声", "職职", "聽听", "肅肃", "腸肠",
    "膚肤", "臉脸", "臨临", "舉举", "舊旧", "藝艺", "處处", "號号", "蟲虫",
    "衝冲", "補补", "裝装", "褲裤", "見见", "規规", "視视", "親亲", "覺觉",
    "觀观", "計计", "討讨", "訓训", "記记", "訪访", "設设", "許许", "訴诉",
    "診诊", "註注", "評评", "詞词", "試试", "詩诗", "話话", "該该", "詳详",
    "認认", "語语", "誠诚", "誤误", "說说", "誰谁", "課课", "調调", "談谈",
    "請请", "論论", "諒谅", "講讲", "謝谢", "證证", "識识", "議议", "護护",
    "讀读", "變变", "讓让", "豐丰", "豬猪", "貝贝", "負负", "財财", "責责",
    "貴贵", "買买", "費费", "資资", "賈贾", "賓宾", "賜赐", "賞赏", "賢贤",
    "賣卖", "質质", "賴赖", "贊赞", "贏赢", "趙赵", "趕赶", "趨趋", "跡迹",
    "踐践", "蹤踪", "車车", "軍军", "軟软", "輕轻", "載载", "較较", "輔辅",
    "輝辉", "輩辈", "輪轮", "輯辑", "輸输", "轄辖", "轉转", "轟轰", "農农",
    "迴回", "連连", "週周", "進进", "遊游", "運运", "過过", "達达", "違违",
    "遠远", "適适", "選选", "遺遗", "遼辽", "邁迈", "還还", "邊边", "邏逻",
    "鄭郑", "釋释", "鐘钟", "鋼钢", "錄录", "錢钱", "錦锦", "鍵键", "鍋锅",
    "鎮镇", "鏡镜", "鐘钟", "鐵铁", "鑰钥", "長长", "閉闭", "開开", "間间",
    "閱阅", "闆板", "關关", "陽阳", "隊队", "階阶", "隨随", "險险", "隱隐",
    "雖虽", "雙双", "雜杂", "雞鸡", "離离", "難难", "雲云", "電电", "靜静",
    "頂顶", "項项", "順顺", "預预", "領领", "頭头", "題题", "顏颜", "願愿",
    "類类", "顧顾", "顯显", "風风", "飛飞", "飯饭", "飲饮", "養养", "館馆",
    "馬马", "駕驾", "騎骑", "驚惊", "體体", "髮发", "鬥斗", "魚鱼", "鮮鲜",
    "鳥鸟", "鳳凤", "鳴鸣", "鴻鸿", "鵬鹏", "鶴鹤", "麗丽", "麥麦", "黃黄",
    "點点", "黨党", "齊齐", "齒齿", "龍龙", "龜龟",
)


def _build_t2s() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in _T2S_PAIRS:
        if len(pair) != 2:
            raise AssertionError(f"繁简表条目必须是两个字符：{pair!r}")
        trad, simp = pair[0], pair[1]
        if trad == simp:
            continue
        mapping.setdefault(trad, simp)
    return mapping


_T2S: dict[str, str] = _build_t2s()

#: ASCII 标点（用于「仅差标点」判定）。
_PUNCT_CHARS = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def variant_key(name: str) -> str:
    """在归一化基础上再剥离「标点 / 序数词 / 繁简」差异的比较键。

    只用于 ``is_suspect``；**不参与** ``normalize_name``——归一化后的名称必须
    保留标点信息，否则「仅差标点」就无法被判为疑似重复（会被误判成精确匹配）。
    纯函数、幂等。
    """
    text = normalize_name(name)
    if not text:
        return ""
    out_chars: list[str] = []
    for ch in text:
        if ch in _PUNCT_CHARS or ch in _ORDINAL_MARKERS:
            continue
        if ch in _CN_DIGITS:
            out_chars.append(_CN_DIGITS[ch])
            continue
        out_chars.append(_T2S.get(ch, ch))
    out = "".join(out_chars)
    for _ in range(_MAX_PASSES):
        nxt = "".join(
            _T2S.get(ch, ch)
            for ch in out
            if ch not in _PUNCT_CHARS and ch not in _ORDINAL_MARKERS
        )
        if nxt == out:
            break
        out = nxt
    return out


def similarity(a: str, b: str) -> float:
    """归一化后的 0.0~1.0 相似度（``difflib`` 比率，确定性）。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


def _as_name(item: Any) -> str:
    """宽容地把列表元素转成名称字符串（str 或带 ``name`` 字段的 dict）。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("name")
        return value if isinstance(value, str) else ""
    return ""


def is_suspect(
    candidate: str,
    existing: Sequence[str] | Iterable[str],
    *,
    threshold: float = 0.75,
) -> bool:
    """归一化后不相等、但高度相近时返回 True（契约 §5）。

    命中任意一条即判为疑似：

    1. ``variant_key`` 相等 —— 仅差标点 / 序数词 / 繁简（"林澈" vs "林澈。"、
       "林澈二号" vs "林澈2"、"钢铁侠" vs "鋼鐵俠"）。
    2. ``difflib`` 相似度 >= ``threshold``（默认 0.75）且两串长度都 >= 2。

    阈值取 0.75 的理由：判为疑似只是「进入人工确认队列」，**绝不自动覆盖**，
    因此宁可稍宽——漏判会让同一角色被重复生成，代价更高。

    注意：归一化后**完全相等**时本函数返回 False —— 那属于精确匹配
    （``MatchOutcome.EXISTS_REUSE``），由 ``prompt.match`` 负责，不是疑似。
    """
    if not isinstance(candidate, str):
        raise TypeError(f"is_suspect 的 candidate 必须是 str，收到 {type(candidate).__name__}")
    cand = normalize_name(candidate)
    if not cand:
        return False
    cand_variant = variant_key(cand)
    for raw in existing:
        other = _as_name(raw)
        if not other:
            continue
        other_norm = normalize_name(other)
        if not other_norm or other_norm == cand:
            continue  # 精确匹配交给 match_candidate
        if cand_variant and cand_variant == variant_key(other_norm):
            return True
        if min(len(cand), len(other_norm)) >= 2 and similarity(cand, other_norm) >= threshold:
            return True
    return False
