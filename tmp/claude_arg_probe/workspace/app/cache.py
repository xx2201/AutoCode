class SimpleCache:
    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value: str):
        self._data[key] = value

    def delete(self, key: str):
        self._data.pop(key, None)

    def clear_prefix(self, prefix: str):
        for key in list(self._data):
            if key.startswith(prefix):
                self._data.pop(key, None)
