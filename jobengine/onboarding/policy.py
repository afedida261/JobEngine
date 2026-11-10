"""Question coverage and limits are application policy, not model discretion."""

from jobengine.models import InterviewExchange, Question

MAX_QUESTIONS = 6
MAX_PROJECT_QUESTIONS = 2
CORE_TOPICS = ("desired_work", "career_goals", "work_style", "sectors")
FALLBACKS = {
    "desired_work": (
        "What kind of work would you most like to spend your week doing, and what would you prefer to do less of?",
        "Prioritize roles around the work you actually want.",
    ),
    "career_goals": (
        "What would you want to learn, accomplish, or get from your next role over the first year?",
        "Understand what a worthwhile next step means to you.",
    ),
    "work_style": (
        "How would you like to work with teammates, customers, or the people you help in your day-to-day work?",
        "Distinguish collaboration and customer-facing preferences.",
    ),
    "sectors": (
        "Which industries or real-world problems motivate you, and are there any you would avoid?",
        "Capture your sector interests without assuming prior experience in them.",
    ),
}


def next_topic(history: list[InterviewExchange], stop_projects=False) -> str | None:
    if len(history) >= MAX_QUESTIONS:
        return None
    covered = {h.topic for h in history}
    for topic in CORE_TOPICS:
        if topic not in covered:
            return topic
    project_count = sum(h.topic == "project" for h in history)
    if stop_projects or project_count >= MAX_PROJECT_QUESTIONS:
        return None
    return "project"


def constrain_question(
    question: Question | None, topic: str | None, history: list[InterviewExchange]
) -> Question | None:
    if topic is None:
        return None
    repeated = question and any(
        h.question.strip().casefold() == question.question.strip().casefold() for h in history
    )
    if topic in CORE_TOPICS:
        if question is None or question.topic != topic or repeated:
            text, purpose = FALLBACKS[topic]
            return Question(question=text, purpose=purpose, topic=topic)
        return question.model_copy(update={"project_key": None})
    asked_projects = {h.project_key.strip().casefold() for h in history if h.project_key}
    if (
        not question
        or question.topic != "project"
        or not question.project_key
        or repeated
        or question.project_key.strip().casefold() in asked_projects
    ):
        return None
    return question
