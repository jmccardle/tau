//! What the driver is handed (`Job`) and what it answers with (`Answer`), with the
//! types they carry. A driver reads jobs and builds answers; nothing here decides.

use pyo3::prelude::*;
use tau_core::step;

use crate::values::{Author, Body, Outcome, Part, Stop, debug_repr};

/// The seven cursor hook points, in the order a step reaches them. It is not an integer.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Hook {
    /// The start of a step.
    PreStep,
    /// Before an input is committed as a `User` node; the subject is its draft.
    PreInput,
    /// Before the sample; the subject is the draft of the reply, which a hook may supply.
    PreSample,
    /// After the sample, before the reply is committed; the subject is the reply's draft.
    PreReply,
    /// After the reply is committed; the subject is its node.
    PostReply,
    /// After the `Results` node is committed; the subject is its node.
    PostResult,
    /// The end of a step; the subject is how it ended.
    PostStep,
}

#[pymethods]
impl Hook {
    /// The name an extension defines the hook by.
    #[getter]
    #[allow(clippy::trivially_copy_pass_by_ref)] // pyo3 cannot move `self` out of a Python object
    fn name(&self) -> &'static str {
        let point = match self {
            Self::PreStep => step::Hook::PreStep,
            Self::PreInput => step::Hook::PreInput,
            Self::PreSample => step::Hook::PreSample,
            Self::PreReply => step::Hook::PreReply,
            Self::PostReply => step::Hook::PostReply,
            Self::PostResult => step::Hook::PostResult,
            Self::PostStep => step::Hook::PostStep,
        };
        point.name()
    }
}

impl From<step::Hook> for Hook {
    fn from(h: step::Hook) -> Self {
        match h {
            step::Hook::PreStep => Self::PreStep,
            step::Hook::PreInput => Self::PreInput,
            step::Hook::PreSample => Self::PreSample,
            step::Hook::PreReply => Self::PreReply,
            step::Hook::PostReply => Self::PostReply,
            step::Hook::PostResult => Self::PostResult,
            step::Hook::PostStep => Self::PostStep,
        }
    }
}

/// How a step ended; the subject of the `post_step` hook.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Ending {
    /// The step ran to its end.
    Done {},
    /// A cursor hook halted it.
    Halted {
        /// The reason the hook gave.
        reason: String,
    },
    /// A hook or the sample failed.
    Failed {
        /// What went wrong.
        error: String,
    },
    /// An abort ended it.
    Aborted {},
}

impl From<step::Ending> for Ending {
    fn from(e: step::Ending) -> Self {
        match e {
            step::Ending::Done => Self::Done {},
            step::Ending::Halted(reason) => Self::Halted { reason },
            step::Ending::Failed(error) => Self::Failed { error },
            step::Ending::Aborted => Self::Aborted {},
        }
    }
}

/// What the current phase will commit as a node. A hook may edit it; none (`None`) commits
/// nothing. Built as `Draft(body, delivers, by)`.
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Draft {
    /// What the node will hold.
    body: Body,
    /// The ids of the scratchpad items the node will consume.
    delivers: Vec<u64>,
    /// Who the node will be written by.
    by: Author,
}

#[pymethods]
impl Draft {
    #[new]
    fn new(body: Body, delivers: Vec<u64>, by: Author) -> Self {
        Self { body, delivers, by }
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

impl From<Ending> for step::Ending {
    fn from(e: Ending) -> Self {
        match e {
            Ending::Done {} => Self::Done,
            Ending::Halted { reason } => Self::Halted(reason),
            Ending::Failed { error } => Self::Failed(error),
            Ending::Aborted {} => Self::Aborted,
        }
    }
}

debug_repr!(RunCall, Ending, Job, Answer, SampleEnd);

impl From<step::Draft> for Draft {
    fn from(d: step::Draft) -> Self {
        Self {
            body: d.body.into(),
            delivers: d.delivers,
            by: d.by.into(),
        }
    }
}

impl From<Draft> for step::Draft {
    fn from(d: Draft) -> Self {
        Self {
            body: d.body.into(),
            delivers: d.delivers,
            by: d.by.into(),
        }
    }
}

/// One call of a batch, handed out in `Job.RunCalls` to be run.
#[pyclass(frozen, eq, hash, get_all, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct RunCall {
    /// The call's id, as in the reply.
    id: String,
    /// The name of the tool to run.
    tool: String,
    /// The arguments exactly as the model produced them.
    args_raw: String,
}

impl From<step::RunCall> for RunCall {
    fn from(c: step::RunCall) -> Self {
        Self {
            id: c.id,
            tool: c.tool,
            args_raw: c.args_raw,
        }
    }
}

/// What a step asks the driver to do next, from `Session.next_job`. Answer it with `Session.answer`.
#[pyclass(frozen, eq, hash, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Job {
    /// Run the extensions' hooks for one hook point.
    Hooks {
        /// The hook point.
        point: Hook,
        /// The subject at `pre_input`, `pre_sample` and `pre_reply`; otherwise `None`.
        draft: Option<Draft>,
        /// The subject at `post_reply` and `post_result`: the node just committed; otherwise `None`.
        node: Option<u64>,
        /// The subject at `post_step`; otherwise `None`.
        ending: Option<Ending>,
    },
    /// Ask the provider for a reply to the cursor's walk.
    Sample {},
    /// Run a batch of calls and answer each with `Answer.CallDone`.
    RunCalls {
        /// The `Assistant` node holding the batch.
        node: u64,
        /// The calls, in call order.
        calls: Vec<RunCall>,
    },
    /// Calls are still out; nothing to do until they are answered.
    Pending {},
    /// The step is over and the cursor is at rest.
    Rest {},
}

impl From<step::Job> for Job {
    fn from(j: step::Job) -> Self {
        match j {
            step::Job::Hooks {
                point,
                draft,
                node,
                ending,
            } => Self::Hooks {
                point: point.into(),
                draft: draft.map(Into::into),
                node,
                ending: ending.map(Into::into),
            },
            step::Job::Sample => Self::Sample {},
            step::Job::RunCalls { node, calls } => Self::RunCalls {
                node,
                calls: calls.into_iter().map(Into::into).collect(),
            },
            step::Job::Pending => Self::Pending {},
            step::Job::Rest => Self::Rest {},
        }
    }
}

/// How a sample ended, as the driver saw it. Core decides what the tree records.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum SampleEnd {
    /// The provider finished the reply.
    #[pyo3(constructor = (stop, reason = None, meta = None))]
    Finished {
        /// Why the reply ended.
        stop: Stop,
        /// The provider's own finish string, or `None`.
        reason: Option<String>,
        /// What the response carried besides its parts, as JSON object text, or `None`.
        meta: Option<String>,
    },
    /// The sample failed.
    #[pyo3(constructor = (error, meta = None))]
    Failed {
        /// What went wrong.
        error: String,
        /// The failure's details as JSON object text, or `None`.
        meta: Option<String>,
    },
    /// The cursor was aborted before the sample finished.
    Cancelled {},
}

impl From<SampleEnd> for step::SampleEnd {
    fn from(e: SampleEnd) -> Self {
        match e {
            SampleEnd::Finished { stop, reason, meta } => Self::Finished {
                stop: stop.into(),
                reason,
                meta,
            },
            SampleEnd::Failed { error, meta } => Self::Failed { error, meta },
            SampleEnd::Cancelled {} => Self::Cancelled,
        }
    }
}

/// What the driver answers a `Job` with. `Session.answer` accepts one only while the job it
/// answers is out; any other raises `WrongAnswer`.
#[pyclass(frozen, eq, hash, from_py_object, module = "tau._core")]
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Answer {
    /// The hooks ran.
    #[pyo3(constructor = (stop = None, draft = None))]
    Hooks {
        /// A halt reason if a hook halted the step, else `None`.
        stop: Option<String>,
        /// The draft after the hooks edited it; `None` commits nothing. At `pre_sample`, a
        /// reply here stands in for the sample.
        draft: Option<Draft>,
    },
    /// A hook raised.
    HookFailed {
        /// What went wrong.
        error: String,
    },
    /// The sample ended.
    Sampled {
        /// What streamed before it ended.
        parts: Vec<Part>,
        /// How it ended.
        end: SampleEnd,
    },
    /// One call of the batch ended.
    CallDone {
        /// The call's id.
        call: String,
        /// How it ended.
        outcome: Outcome,
        /// `Tools` if the tool ran unedited, else the extension that answered or last edited it.
        by: Author,
    },
}

impl From<Answer> for step::Answer {
    fn from(f: Answer) -> Self {
        match f {
            Answer::Hooks { stop, draft } => Self::Hooks {
                stop,
                draft: draft.map(Into::into),
            },
            Answer::HookFailed { error } => Self::HookFailed(error),
            Answer::Sampled { parts, end } => Self::Sampled {
                parts: parts.into_iter().map(Into::into).collect(),
                end: end.into(),
            },
            Answer::CallDone { call, outcome, by } => Self::CallDone {
                call,
                outcome: outcome.into(),
                by: by.into(),
            },
        }
    }
}
