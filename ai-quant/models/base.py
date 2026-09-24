from abc import ABC, abstractmethod


class BaseModel(ABC):

    @abstractmethod
    def analyze(self, context):
        pass
