"""School adapter registry."""

from src.schools.princeton import PrincetonAdapter


def get_adapter(slug: str):
    if slug == "princeton":
        return PrincetonAdapter()
    raise ValueError(f"Unsupported school adapter: {slug}")

