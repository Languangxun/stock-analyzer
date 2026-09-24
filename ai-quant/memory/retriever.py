from models.embedding import OllamaEmbedding
from memory.vector import VectorMemory


class MemoryRetriever:


    def __init__(self):

        self.embedding = OllamaEmbedding()

        self.memory = VectorMemory()



    def build_query(self, features):

        return (

            f"价格:{features.get('price')} "

            f"涨跌:{features.get('change_1d')} "

            f"趋势:{features.get('trend')} "

            f"波动:{features.get('volatility')}"

        )



    def search(self, features, limit=3):

        text = self.build_query(
            features
        )


        vector = self.embedding.embed(
            text
        )


        return self.memory.search(
            vector,
            limit
        )



if __name__ == "__main__":


    retriever = MemoryRetriever()


    features = {

        "price":1.08,

        "change_1d":2.8,

        "trend":"side",

        "volatility":0.04

    }


    result = retriever.search(
        features
    )


    for item in result:

        print(
            item
        )
