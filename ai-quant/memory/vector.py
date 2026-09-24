import json
import numpy as np


class VectorMemory:


    def __init__(
        self,
        path="memory/embeddings.json"
    ):

        self.path = path



    def cosine(self,a,b):

        a=np.array(a)

        b=np.array(b)


        return float(

            np.dot(a,b)

            /
            (
                np.linalg.norm(a)
                *
                np.linalg.norm(b)
            )

        )



    def load(self):

        with open(
            self.path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)



    def search(
        self,
        vector,
        limit=3
    ):


        data=self.load()


        result=[]


        for item in data:


            score=self.cosine(

                vector,

                item["vector"]

            )


            result.append({

                "score":score,

                "trade":item["trade"]

            })



        result.sort(

            key=lambda x:x["score"],

            reverse=True

        )


        return result[:limit]



if __name__=="__main__":

    memory=VectorMemory()

    print(
        len(memory.load())
    )
