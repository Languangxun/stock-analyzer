import yaml


def load_model_config():

    with open(
        "config/model.yaml",
        "r",
        encoding="utf-8"
    ) as f:

        return yaml.safe_load(f)
