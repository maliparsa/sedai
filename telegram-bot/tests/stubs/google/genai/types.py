class GenerateContentConfig:
    def __init__(self, system_instruction=None, **kwargs):
        self.system_instruction = system_instruction
        for k, v in kwargs.items():
            setattr(self, k, v)


class Part:
    def __init__(self, data=None, mime_type=None):
        self.data = data
        self.mime_type = mime_type

    @classmethod
    def from_bytes(cls, data=None, mime_type=None):
        return cls(data=data, mime_type=mime_type)
