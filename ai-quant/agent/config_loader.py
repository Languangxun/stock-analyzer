from pathlib import Path
import yaml


BASE_DIR = Path(__file__).resolve().parent.parent


def load_watchlist():
    file = BASE_DIR / "data" / "fund" / "watchlist.yaml"

    with open(file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data["funds"]


if __name__ == "__main__":
    funds = load_watchlist()

    for fund in funds:
        print(fund["name"])
