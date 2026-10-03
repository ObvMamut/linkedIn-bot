"""Draft grounding: every factual sentence must cite confirmed facts that support it."""

import re
from collections.abc import Mapping

from pydantic import Field

from linkedin_bot.domain.models import CandidateFact, Contract

COURTESY = frozenset(
    {
        "thank you for your time.",
        "thank you for considering my application.",
        "i look forward to hearing from you.",
        "kind regards",
        "sincerely",
    }
)


class DraftClaim(Contract):
    text: str = Field(min_length=1, max_length=1200)
    fact_ids: tuple[str, ...] = ()


class CoverLetterDraft(Contract):
    paragraphs: tuple[DraftClaim, ...] = Field(min_length=1, max_length=8)

    def render(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs) + "\n"


def significant(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9+#]+", text.lower()) if len(word) > 3}


def grounding_violations(draft: CoverLetterDraft, facts: Mapping[str, CandidateFact]) -> list[str]:
    """Empty means every claim cites confirmed facts that contain its numbers and a key term.

    This catches fabricated figures, uncited claims and unconfirmed facts. It is not a proof
    of truth: a paraphrase that reuses a cited word can still overstate a fact.
    """
    violations: list[str] = []
    for claim in draft.paragraphs:
        cited = []
        for fact_id in claim.fact_ids:
            fact = facts.get(fact_id)
            if fact is None:
                violations.append(f"unknown_fact:{fact_id}")
            elif fact.status != "confirmed" or fact.value is None:
                violations.append(f"unconfirmed_fact:{fact_id}")
            else:
                cited.append(fact.value)
        if not claim.fact_ids:
            if claim.text.strip().lower() not in COURTESY:
                violations.append("ungrounded_claim")
            continue
        evidence = " ".join(cited)
        for number in sorted(set(re.findall(r"\d+", claim.text))):
            if number not in re.findall(r"\d+", evidence):
                violations.append(f"unsupported_number:{number}")
        if cited and not significant(claim.text) & significant(evidence):
            violations.append("unsupported_claim")
    return sorted(set(violations))
