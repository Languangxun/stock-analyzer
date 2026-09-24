import json
import os


class MemoryReader:


    def __init__(self, path="memory/trades.json"):

        self.path = path



    def load(self):

        if not os.path.exists(
            self.path
        ):

            return []


        with open(
            self.path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)



    def recent(self, limit=5):

        trades = self.load()


        return trades[-limit:]



    def summary(self):

        trades = self.load()


        return {

            "total_trades": len(trades),

            "recent_trades": trades[-5:]

        }



if __name__ == "__main__":


    memory = MemoryReader()


    print(
        memory.summary()
    )

