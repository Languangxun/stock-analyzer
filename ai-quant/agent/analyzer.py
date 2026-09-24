from data.market.mock_provider import get_mock_market


def analyze_market():
    market = get_mock_market()

    report = []

    for item in market:
        if item.change_percent >= 2:
            signal = "强势"
        elif item.change_percent <= -2:
            signal = "弱势"
        else:
            signal = "震荡"

        report.append({
            "name": item.name,
            "change": item.change_percent,
            "signal": signal,
        })

    return report


if __name__ == "__main__":
    result = analyze_market()

    print("=== 市场分析 ===")

    for item in result:
        print(
            f"{item['name']} "
            f"{item['change']}% "
            f"{item['signal']}"
        )
