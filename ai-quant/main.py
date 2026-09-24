import yaml


def load_config():

    with open(
        "config/system.yaml",
        encoding="utf-8"
    ) as f:

        return yaml.safe_load(f)



def main():

    config = load_config()

    print(
        "AI Quant System Ready"
    )

    print(
        config
    )


if __name__ == "__main__":

    main()
