import os

import requests


class OllamaEmbedding:
    def __init__(
        self,
        model=None,
        base_url=None,
    ):
        self.model = model or os.environ.get(
            "OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b"
        )
        self.base_url = (
            base_url or os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434"
            )
        ).rstrip("/")

    def embed(self, text):
        response = requests.post(
            f"{self.base_url}/api/embeddings",
            json={
                "model": self.model,
                "prompt": text,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["embedding"]
