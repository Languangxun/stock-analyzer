WATCH_MAP = {
    "半导体ETF": "半导体",
    "半导体": "半导体",

    "通信ETF": "通信",
    "通信": "通信",

    "人工智能ETF": "人工智能",
    "人工智能": "人工智能",

    "消费电子ETF": "消费电子",
    "消费电子": "消费电子",

    "银行ETF": "银行",
    "银行": "银行",
}


def normalize_target(target):

    return WATCH_MAP.get(
        target,
        target
    )
