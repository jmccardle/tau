"""A hand-run job loop for the tests: perform each job from a script, answer it back."""

from collections.abc import Callable, Iterator

from tau_next import (
    Answer,
    Author,
    Body,
    Draft,
    Job,
    Outcome,
    Part,
    Session,
    Stop,
)


def send(session: Session, cursor: int, client: str, text: str) -> int:
    """Input with auto-step on, as a frontend sends."""
    item = session.input(cursor, client, text)
    session.set_auto(cursor, True)
    return item


def say(text: str) -> Answer:
    reply = Draft(Body.Assistant([Part.Text(text)], Stop.End), Author.Provider(), [])
    return Answer.Sampled(reply, None)


def call(*calls: tuple[str, str]) -> Answer:
    parts = [Part.Call(id, tool, "{}") for id, tool in calls]
    return Answer.Sampled(Draft(Body.Assistant(parts, Stop.Tools), Author.Provider(), []), None)


def run(
    session: Session,
    cursor: int,
    samples: Iterator[Answer] | None = None,
    now: int = 0,
    on_job: Callable[[Job], list[Answer] | None] | None = None,
) -> list[Job]:
    """Drive `cursor` to rest. Hooks leave the draft as it came, samples come from
    `samples`, calls succeed with their id. `on_job` may answer a job."""
    seen: list[Job] = []
    for _ in range(200):
        job = session.next_job(cursor)
        seen.append(job)
        if isinstance(job, Job.Rest):
            return seen
        answers = on_job(job) if on_job else None
        if answers is None:
            answers = default_answers(job, samples)
        for answer in answers:
            session.answer(cursor, answer, now)
    raise AssertionError(f"no rest: {seen}")


def default_answers(job: Job, samples: Iterator[Answer] | None) -> list[Answer]:
    match job:
        case Job.Hooks(draft=draft):
            return [Answer.Hooks(None, draft)]
        case Job.Sample():
            assert samples is not None, "a sample with no script"
            return [next(samples)]
        case Job.RunCalls(calls=calls):
            return [
                Answer.CallDone(c.id, Outcome.Ok(f"ran {c.tool}"), Author.Tools()) for c in calls
            ]
    raise AssertionError(f"no default answer for {job}")
