from dataclasses import dataclass


class TransientGenerationError(Exception):
    pass


class PermanentGenerationError(Exception):
    pass


@dataclass(frozen=True)
class Draft:
    text: str
    sources: list[dict]


def generate_draft(support_request):
    raise PermanentGenerationError(
        "The real draft generator is not configured; checkpoint B is required."
    )
