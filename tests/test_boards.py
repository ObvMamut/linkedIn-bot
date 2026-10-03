import asyncio

from linkedin_bot.workflows.boards import find_board, slug_candidates


def test_slug_candidates_cover_common_spellings_without_duplicates():
    assert slug_candidates("Acme Rockets, Inc.") == ["acmerockets", "acme-rockets", "acme"]
    assert slug_candidates("Palantir") == ["palantir"]
    assert slug_candidates("  ") == []
    assert slug_candidates("A&B  Security GmbH") == ["absecurity", "a-b-security", "a"]


def test_find_board_returns_the_first_public_board_and_bounds_requests():
    seen = []

    async def probe(provider, slug):
        seen.append((provider, slug))
        return (provider, slug) == ("lever", "acme-rockets")

    assert asyncio.run(find_board("Acme Rockets", probe)) == ("lever", "acme-rockets")
    assert seen[0] == ("greenhouse", "acmerockets")  # tried in a fixed, predictable order

    seen.clear()

    async def nothing(provider, slug):
        seen.append((provider, slug))
        return False

    assert asyncio.run(find_board("Some Long Firm Name Here Ltd", nothing)) is None
    assert len(seen) <= 9  # three spellings across three providers at most
