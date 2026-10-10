"""新闻过滤：词表 + 五层过滤逻辑。

词表可被 config.json 同名字段覆盖（market_words / substance_words /
rumor_words / trusted_sources / digest_kill_words），为空则用这里的默认表。
"""
import re
from typing import Set

# 行情噪音过滤：标题含行情词但不含任何实质动作词 => 盘中涨跌播报，丢弃
# （「智谱宣布整改完成，股价拉升翻红」这类有实质内容的会因含动作词而保留）
DEFAULT_MARKET_WORDS = [
    "收评", "午盘", "盘前", "早盘", "高开", "低开", "涨超", "跌超", "涨逾",
    "跌逾", "大涨", "大跌", "拉升", "翻红", "走低", "走高", "创新高", "创新低",
    "涨停", "跌停", "市值", "股价", "恒指", "科指", "港股通", "涨幅", "跌幅",
    "领涨", "领跌", "异动",
    # 价格对比/导购格调（无实质事件的评论稿）
    "起售价", "售价", "定价", "对比", "对标", "吊打", "碾压", "值得买",
    "值不值", "怎么选", "选谁",
]
DEFAULT_SUBSTANCE_WORDS = [
    "发布", "上线", "上架", "宣布", "推出", "开源", "回应", "公告", "业绩",
    "财报", "融资", "合作", "任命", "收购", "新品", "发布会", "整改", "停牌",
    "复牌", "战略", "签约", "重组", "投资", "成立", "突破", "评测", "实测",
    "体验", "招募", "降价", "免费", "涨价", "计划", "披露", "澄清", "否认",
    "证实", "入驻", "接入", "入选", "认证",
    "国行", "发售", "开售", "预售", "上市", "首发", "政策", "调整",
    "败诉", "胜诉", "诉讼", "裁决", "判赔", "赔偿",
]

# 标题命中即标记为「传闻」（未经证实），低优先级静默推送，不会被误导
DEFAULT_RUMOR_WORDS = ["爆料", "传闻", "据传", "网传", "消息人士", "知情人士",
                       "被曝", "疑似", "泄露", "流出", "或将"]

# Google News 条目自带的来源名，命中即标注「权威」
DEFAULT_TRUSTED_SOURCES = [
    "财新", "财联社", "证券时报", "上海证券报", "中国证券报", "界面", "澎湃",
    "新华", "人民日报", "中新网", "第一财经", "每日经济新闻", "21世纪经济报道",
    "科创板日报", "36氪", "虎嗅", "钛媒体", "智东西", "量子位", "机器之心",
    "新浪科技", "腾讯科技", "网易科技", "TechWeb", "雷峰网", "IT之家",
]

# 速览合辑 / SEO软文 / 类比框架 / 流量标题党的特征词，命中即丢弃
# （速览类已由「丨/；」分隔符规则覆盖，这里不再收"日报/早报"等词，
#   否则会误杀"北京日报社与XX合作"这类真新闻）
DIGEST_KILL_WORDS = ("盘点", "榜单", "服务商", "沽空", "净买入", "净卖出",
                     "情报站", "美版", "中国版", "平替", "翻版", "冒牌",
                     "王炸", "大招", "抢到赚到", "手慢无", "速抢", "炸裂",
                     "逆天", "杀疯", "真香", "联播", "财闻", "早知道",
                     "一周数据", "今日必读")

# 同一事件被多家媒体报道的标题相似度阈值（字符二元组 Jaccard）。
# 实测分布：同事件 0.50~0.58，不同事件 ≤0.24，鸿沟明显，0.35 有大安全余量
# （曾有一对同事件标题算出 0.4998，卡在 0.5 阈值下漏网）
NEWS_DUP_THRESHOLD = 0.35


def _bigrams(s: str) -> Set[str]:
    s = re.sub(r"[\s!！？?：:，,。.、()（）·|｜；;\"'‘’“”]+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar_title(a: str, b: str) -> bool:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return False
    if len(A & B) / len(A | B) < NEWS_DUP_THRESHOLD:
        return False
    # 数字组完全不同（如 GLM-5.4 vs GLM-5.5、8999元 vs 8.18亿）→ 不同事件，
    # 防止高字面相似度的"不同版本/不同金额"新闻被误合并；有共同数字则不算冲突
    na = set(re.findall(r"\d+(?:\.\d+)?", a))
    nb = set(re.findall(r"\d+(?:\.\d+)?", b))
    if na and nb and not (na & nb):
        return False
    return True


def is_market_noise(title: str, cfg: dict) -> bool:
    """盘中行情播报（智谱涨超X%之类）和转载站关键词堆砌标题（含≥3个竖线）。"""
    if cfg.get("news_market_filter", True):
        market = cfg.get("market_words") or DEFAULT_MARKET_WORDS
        substance = cfg.get("substance_words") or DEFAULT_SUBSTANCE_WORDS
        if any(w in title for w in market) and not any(w in title for w in substance):
            return True
    # "xxx|北京商报|百度百科|大模型|智谱华章|手机新浪网"式堆砌转载标题
    return title.count("|") + title.count("｜") >= 3


def is_offtopic(title: str, query: str, cfg: dict) -> bool:
    """关键词不是标题主语：速览合辑、类比比喻（"美版X"/"X时刻"）、
    顺带点名（出现在后半句）、SEO软文 → 丢弃。"""
    if not cfg.get("news_subject_filter", True):
        return False
    tokens = [t for t in query.split() if t]
    if not tokens:
        return False
    # 速览合辑：丨分隔、分号串联多条；软文/流量词（可用 config 的 digest_kill_words 覆盖）
    if "丨" in title or "；" in title or ";" in title:
        return True
    kill_words = cfg.get("digest_kill_words") or DIGEST_KILL_WORDS
    if any(w in title for w in kill_words):
        return True
    # 类比/比喻用法："DeepSeek时刻"、"美版 DeepSeek"（忽略空格）
    nospace = title.replace(" ", "")
    if any(f"{t}时刻" in nospace for t in tokens):
        return True
    # 主语判定：前10字符内首次出现、或出现≥2次、或含实质动作词
    if any(t in title[:10] for t in tokens):
        return False
    if sum(title.count(t) for t in tokens) >= 2:
        return False
    substance = cfg.get("substance_words") or DEFAULT_SUBSTANCE_WORDS
    return not any(w in title for w in substance)
