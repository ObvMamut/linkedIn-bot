"""Small helpers shared by the button UI and the digest."""


def split_job_query(text: str) -> tuple[str, str | None]:
    """'security intern in Berlin' -> ('security intern', 'Berlin')."""
    words, separator, place = text.rpartition(" in ")
    if separator and words.strip() and place.strip():
        return words.strip(), place.strip()
    return text.strip(), None
