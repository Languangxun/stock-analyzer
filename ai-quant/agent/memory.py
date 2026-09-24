from pathlib import Path
import json
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent.parent


def save_daily_report(report):
    today = datetime.now().strftime("%Y-%m-%d")

    folder = BASE_DIR / "memory" / "daily"
    folder.mkdir(parents=True, exist_ok=True)

    file = folder / f"{today}.json"

    data = {
        "time": datetime.now().isoformat(),
        "report": report,
    }

    with open(file, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    return file


if __name__ == "__main__":
    test = [
        {
            "name": "半导体",
            "signal": "观察",
            "change": 1.5
        }
    ]

    path = save_daily_report(test)

    print(f"saved: {path}")
