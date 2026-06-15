"""Emergency resource catalog (US1 / FR-003) — code constants, not a DB table.

Mirrors the voice/model catalog-as-code pattern. ``raise_crisis`` looks up the
resource for a detected crisis category and returns its spoken script for the agent
to speak; the flag records which resource was offered. Numbers are the standard US
crisis lines: 988 (Suicide & Crisis Lifeline), 911 (emergency), the Eldercare Locator
for Adult Protective Services, and Poison Control (1-800-222-1222).
"""

from dataclasses import dataclass

from usan_api.schemas.crisis import CrisisCategory


@dataclass(frozen=True)
class EmergencyResource:
    """One crisis category's resource: the number to give and the words to speak."""

    category: CrisisCategory
    label: str
    number: str
    spoken_script: str


_RESOURCES: dict[str, EmergencyResource] = {
    "suicidal": EmergencyResource(
        category="suicidal",
        label="988 Suicide & Crisis Lifeline",
        number="988",
        spoken_script=(
            "I'm really concerned about you, and I want you to get support right now. "
            "Please call or text 988 — the Suicide and Crisis Lifeline — to talk with "
            "someone who can help. If you are in immediate danger, please call 911."
        ),
    ),
    "medical": EmergencyResource(
        category="medical",
        label="911 Emergency Services",
        number="911",
        spoken_script=(
            "This sounds like a medical emergency. Please call 911 right away, or ask "
            "someone near you to call for you. I'll make sure your family is notified too."
        ),
    ),
    "abuse": EmergencyResource(
        category="abuse",
        label="Adult Protective Services (Eldercare Locator)",
        number="1-800-677-1116",
        spoken_script=(
            "I'm concerned about your safety. Adult Protective Services can help — you "
            "can reach them through the Eldercare Locator at 1-800-677-1116. If you are "
            "in immediate danger, please call 911."
        ),
    ),
    "confusion": EmergencyResource(
        category="confusion",
        label="911 Emergency Services",
        number="911",
        spoken_script=(
            "I'm a little worried about how you're feeling right now, and I want to make "
            "sure you're safe. If you feel unwell or unsafe, please call 911, and I'll "
            "let your family know to check on you."
        ),
    ),
    "overdose": EmergencyResource(
        category="overdose",
        label="Poison Control",
        number="1-800-222-1222",
        spoken_script=(
            "This could be serious. Please call Poison Control right now at "
            "1-800-222-1222, and if you feel very unwell, call 911. I'll notify your "
            "family as well."
        ),
    ),
}


def get_resource(category: CrisisCategory) -> EmergencyResource:
    """The emergency resource for a crisis category. Raises KeyError on an unknown one
    (callers validate ``category`` against the Pydantic CrisisCategory first)."""
    return _RESOURCES[category]


# Categories whose numbers compose the on-request informational SMS (US7 / FR-041),
# general-emergency first. Drawn from the SAME catalog the crisis flow uses so the
# numbers never drift; ``send_info_sms`` texts this list to the contact on request.
_INFO_SMS_CATEGORIES: tuple[CrisisCategory, ...] = ("medical", "suicidal", "overdose", "abuse")


def informational_sms_body() -> str:
    """A PHI-free SMS body listing the standard emergency/helpline numbers (FR-041).

    Built from the resource catalog (label + number only), so it carries NO clinical
    content and the numbers stay single-sourced. Kept within one SMS segment-budget.
    """
    parts = [f"{r.label}: {r.number}" for r in (get_resource(c) for c in _INFO_SMS_CATEGORIES)]
    return "USAN Retirement helpful numbers — " + "; ".join(parts) + ". Keep these handy. — USAN"
