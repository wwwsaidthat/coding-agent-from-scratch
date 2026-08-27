"""Small text helpers used by the agent demonstration."""


def slugify(text: str) -> str:
    """Convert text into a lowercase URL slug."""
    return text.lower().replace(" ", "-")
