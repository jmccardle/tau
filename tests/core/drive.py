"""A hand-run job loop for the tests: perform each job from a script, feed it back."""

from collections.abc import Callable, Iterator

from tau_next import (
    Author,
    Body,
    Draft,
    Fed,
    Job,
    Outcome,
    Part,
    Session,
    Stop,
)


def say(text: str) -> Fed:
    reply = Draft(Body.Assistant([Part.Text(text)], Stop.End), Author.Provider(), [])
    return Fed.Sampled(reply, None)


def call(*calls: tuple[str, str]) -> Fed:
    parts = [Part.Call(id, tool, "{}") for id, tool in calls]
    return Fed.Sampled(Draft(Body.Assistant(parts, Stop.Tools), Author.Provider(), []), None)


def run(
    session: Session,
    cursor: int,
    samples: Iterator[Fed] | None = None,
    now: int = 0,
    on_job: Callable[[Job], list[Fed] | None] | None = None,
) -> list[Job]:
    """Drive `cursor` to rest. Hooks leave the draft as it came, samples come from
    `samples`, calls succeed with their id, the throttle passes. `on_job` may answer a job."""
    seen: list[Job] = []
    for _ in range(200):
        job = session.next_job(cursor)
        seen.append(job)
        if isinstance(job, Job.Rest):
            return seen
        answers = on_job(job) if on_job else None
        if answers is None:
            answers = default_answers(job, samples)
        for fed in answers:
            session.feed(cursor, fed, now)
    raise AssertionError(f"no rest: {seen}")


def default_answers(job: Job, samples: Iterator[Fed] | None) -> list[Fed]:
    match job:
        case Job.Hooks(draft=draft):
            return [Fed.Hooks(None, draft)]
        case Job.Sample():
            assert samples is not None, "a sample with no script"
            return [next(samples)]
        case Job.RunCalls(calls=calls):
            return [Fed.CallDone(c.id, Outcome.Ok(f"ran {c.tool}"), Author.Tools()) for c in calls]
        case Job.Wait():
            return [Fed.Waited()]
    raise AssertionError(f"no default answer for {job}")
