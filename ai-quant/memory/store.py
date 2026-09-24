from datetime import datetime


class MemoryStore:

    def __init__(self):
        self.records = []


    def add(self, data):

        item = {
            "time": datetime.now(),
            "data": data
        }

        self.records.append(item)


    def all(self):

        return self.records
