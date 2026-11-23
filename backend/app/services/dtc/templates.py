"""The fallback explanation: built from the reviewed definition alone, no LLM.

Used when there's no LLM key, the monthly budget is spent, the model's answer failed
validation twice, or the code isn't in our list. It's plainer than a personalised answer
but always correct: the facts and the severity come straight from the definition.
"""

from app.llm.schemas import Cause, DtcExplanation
from app.services.dtc.context import DtcContext

PENDING = (
    "Your car's computer noticed something once but hasn't confirmed it. The warning light "
    "is still off."
)  # PRD 5 wording
WHAT_TO_DO = {
    "info": "Nothing urgent. Mention it at your next service.",
    "watch": "Keep driving normally, and have it checked at your next service.",
    "service_soon": (
        "Have it checked within the next few days. Driving on can make it worse or waste fuel."
    ),
    "stop_safely": (
        "Stop when it is safe to do so and switch off the engine. Don't keep driving; get help."
    ),
}
QUESTIONS = [
    "What did you find when you tested for this code?",
    "Which of the likely causes did you rule out, and how?",
]


def template_explanation(ctx: DtcContext) -> DtcExplanation:
    d = ctx.definition
    if d is None:
        return DtcExplanation(
            summary=(
                f"Your car reported fault code {ctx.code}. It isn't in AutoCare's list yet, "
                "often because it's specific to the car's maker."
            ),
            severity="watch",
            likely_causes=[
                Cause(
                    cause="Not known yet",
                    why="This code isn't in AutoCare's list of generic codes.",
                )
            ],
            what_to_do_now=(
                "If the car drives normally, have the code read with a dealer-level scan tool "
                "at your next service."
            ),
            questions_for_mechanic=[f"What does code {ctx.code} mean on this car?", *QUESTIONS],
            confidence="low",
        )
    lead = f"{PENDING} What it noticed: " if ctx.status == "pending" else ""
    summary = f"{lead}{d.description}"
    if d.escalation:
        summary += f" Note: {d.escalation}."
    return DtcExplanation(
        summary=summary[:600],
        severity=d.severity,
        likely_causes=[
            Cause(cause=c[:200], why="The most common cause." if i == 0 else "A common cause.")
            for i, c in enumerate(d.causes[:5])
        ],
        what_to_do_now=WHAT_TO_DO[d.severity],
        questions_for_mechanic=QUESTIONS,
        confidence="medium",
    )
