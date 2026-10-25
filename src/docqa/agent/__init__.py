"""Controlled document answers and externally consumable evidence sessions."""


def answer_question(*args, **kwargs):
    from docqa.agent.pipeline import answer_question as implementation

    return implementation(*args, **kwargs)
