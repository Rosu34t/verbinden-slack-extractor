"""Deterministic in-process provider for tests."""


class FakeLlm:
    def __init__(self, response: str = '{}') -> None:
        self._response = response
        self._prompts: tuple[str, ...] = ()

    @property
    def prompts(self) -> tuple[str, ...]:
        return self._prompts

    def generate_json(self, prompt: str) -> str:
        self._prompts = (*self._prompts, prompt)
        return self._response

    def close(self) -> None:
        pass
