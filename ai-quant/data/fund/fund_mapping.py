"""场外 ETF 联接 C 类基金映射。

AI 只输出行业名（半导体/通信/人工智能/银行/消费电子），
mapping 层决定具体基金代码与场内信号 ETF。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class FundInfo:
    symbol: str        # 行业名（AI 输出用）
    fund_code: str     # 场外联接 C 类代码
    fund_name: str
    etf_code: str      # 场内 ETF（仅作市场信号）
    etf_market: str    # sh / sz


FUND_MAP = {
    "半导体": FundInfo("半导体", "007301",
                   "国联安中证全指半导体产品与设备ETF联接C",
                   "512480", "sh"),
    "通信": FundInfo("通信", "007818",
                 "国泰中证全指通信设备ETF联接C",
                 "515880", "sh"),
    "人工智能": FundInfo("人工智能", "012734",
                    "易方达中证人工智能主题ETF联接C",
                    "159819", "sz"),
    "银行": FundInfo("银行", "006697",
                 "华宝银行ETF联接C",
                 "512800", "sh"),
    "消费电子": FundInfo("消费电子", "018301",
                    "华夏消费电子ETF联接C",
                    "159732", "sz"),
    "医药": FundInfo("医药", "007883",
                 "汇添富中证医药卫生ETF联接C",
                 "512010", "sh"),
    "证券": FundInfo("证券", "007882",
                 "易方达证券公司ETF联接C",
                 "512880", "sh"),
    "红利": FundInfo("红利", "012644",
                 "华夏中证红利ETF联接C",
                 "510880", "sh"),
    "创业板": FundInfo("创业板", "006249",
                   "华夏创业板ETF联接C",
                   "159915", "sz"),
    "科创50": FundInfo("科创50", "011613",
                   "华夏科创50ETF联接C",
                   "588000", "sh"),
}


def resolve(symbol: str):
    """行业名 -> FundInfo；未知返回 None。"""
    return FUND_MAP.get(symbol)


def by_fund_code(code: str):
    for info in FUND_MAP.values():
        if info.fund_code == code:
            return info
    return None


def by_etf_code(code: str):
    for info in FUND_MAP.values():
        if info.etf_code == code:
            return info
    return None
