//! The connection: JSON-RPC 2.0 over a WebSocket over the server's Unix socket
//! (docs/guide/protocol.md, "How it travels").
//!
//! One reader task receives every message and keeps the order the server sent them in: an event
//! is queued before the response that depends on it is delivered. Calls are concurrent and are
//! matched to their responses by `id`. The envelope is the standard one; every params, result,
//! event and error inside it is a generated type (`crate::wire`).

use std::{
    collections::HashMap,
    fmt,
    io::ErrorKind,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::{
    net::UnixStream,
    sync::{mpsc, oneshot},
};
use tokio_tungstenite::tungstenite::Message;

use crate::wire::{self, Call};

const PROBE: std::time::Duration = std::time::Duration::from_secs(2);

/// What the server sends that is not a response.
#[derive(Debug)]
pub enum Incoming {
    Event(wire::Event),
    /// The notification `end`: the last message of a connection.
    End(wire::End),
    /// The connection is gone or the server said something this client cannot read.
    Lost(String),
}

/// Why a call did not return its result.
#[derive(Debug)]
pub enum CallError {
    /// The server answered with a JSON-RPC error.
    Server(wire::Error),
    /// The connection ended before the answer.
    Closed,
    /// The answer did not fit the type the schema gives it.
    Unreadable(String),
}

impl CallError {
    /// The error's code, if the server sent one.
    pub fn code(&self) -> Option<i64> {
        match self {
            Self::Server(e) => Some(*e.code),
            _ => None,
        }
    }

    /// The `data` object of a server error, as JSON.
    pub fn data(&self) -> Option<Value> {
        match self {
            Self::Server(e) => Some(Value::Object(e.data.clone())),
            _ => None,
        }
    }
}

impl fmt::Display for CallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Server(e) => f.write_str(&e.message),
            Self::Closed => f.write_str("the connection to the server ended"),
            Self::Unreadable(why) => {
                write!(f, "the server's answer does not fit the schema: {why}")
            }
        }
    }
}

type Reply = Result<Value, wire::Error>;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Reply>>>>;

/// A cloneable handle for making calls.
#[derive(Clone)]
pub struct Link {
    out: mpsc::UnboundedSender<Message>,
    pending: Pending,
    next: Arc<AtomicU64>,
}

impl fmt::Debug for Link {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Link")
    }
}

/// A connection that has said `hello`: the handle, the stream of what the server sends, and the
/// server's `Welcome`.
pub struct Connected {
    pub link: Link,
    pub incoming: mpsc::UnboundedReceiver<Incoming>,
    pub welcome: wire::Welcome,
}

/// Dial `socket` and say `hello` as `client`. The error is the sentence to print: the server's own
/// message for a refused `hello` (`ProtocolMismatch` says which protocol the server speaks).
pub async fn connect(socket: &Path, client: &str) -> Result<Connected, String> {
    let stream = UnixStream::connect(socket)
        .await
        .map_err(|e| format!("cannot connect to {}: {e}", socket.display()))?;
    let (ws, _) = tokio_tungstenite::client_async("ws://localhost/", stream)
        .await
        .map_err(|e| format!("{} did not accept a WebSocket: {e}", socket.display()))?;
    let (mut sink, mut source) = ws.split();

    let (out, mut outbox) = mpsc::unbounded_channel::<Message>();
    tokio::spawn(async move {
        while let Some(message) = outbox.recv().await {
            if sink.send(message).await.is_err() {
                break;
            }
        }
        let _ = sink.close().await;
    });

    let pending: Pending = Arc::default();
    let (tell, incoming) = mpsc::unbounded_channel();
    let waiting = Arc::clone(&pending);
    tokio::spawn(async move {
        let lost = loop {
            match source.next().await {
                Some(Ok(Message::Text(text))) => {
                    if let Err(why) = dispatch(text.as_str(), &waiting, &tell) {
                        break why;
                    }
                }
                Some(Ok(Message::Close(_))) | None => {
                    break "the server closed the connection".to_owned();
                }
                Some(Ok(_)) => {}
                Some(Err(e)) => break format!("the connection failed: {e}"),
            }
        };
        waiting
            .lock()
            .expect("pending is never held across a panic")
            .clear();
        let _ = tell.send(Incoming::Lost(lost));
    });

    let link = Link {
        out,
        pending,
        next: Arc::default(),
    };
    let hello = wire::HelloParams {
        protocol: wire::PROTOCOL,
        client: client.to_owned(),
        buffer: None,
    };
    let welcome = link.call(&hello).await.map_err(|e| match e.code() {
        Some(wire::code::PROTOCOL_MISMATCH) => format!("protocol mismatch: {e}"),
        _ => format!("hello failed: {e}"),
    })?;
    Ok(Connected {
        link,
        incoming,
        welcome: welcome.0,
    })
}

/// Whether a server answers at `socket`, as `tau` decides it for a serve record: `Ok(false)` if
/// the socket is gone or nothing listens (a crash left the record), `Err` for anything that does
/// not prove it dead (no `hello` answered in two seconds, a peer that is no server).
pub async fn answers(socket: &Path) -> Result<bool, String> {
    match UnixStream::connect(socket).await {
        Ok(stream) => drop(stream),
        Err(e) if matches!(e.kind(), ErrorKind::NotFound | ErrorKind::ConnectionRefused) => {
            return Ok(false);
        }
        Err(e) => return Err(format!("cannot probe {}: {e}", socket.display())),
    }
    match tokio::time::timeout(PROBE, connect(socket, "tau-ratatui:probe")).await {
        Ok(Ok(_)) => Ok(true),
        Ok(Err(e)) => Err(format!("{} did not answer hello: {e}", socket.display())),
        Err(_) => Err(format!(
            "{} did not answer hello in {PROBE:?}; it may be busy",
            socket.display()
        )),
    }
}

/// Route one message: a response to its caller, an event or `end` to the stream. `Err` is why the
/// connection cannot go on.
fn dispatch(
    text: &str,
    pending: &Pending,
    tell: &mpsc::UnboundedSender<Incoming>,
) -> Result<(), String> {
    let message: Value = serde_json::from_str(text)
        .map_err(|e| format!("the server sent what is not JSON ({e}): {text}"))?;
    let params = || message.get("params").cloned().unwrap_or(Value::Null);
    match message.get("method").and_then(Value::as_str) {
        Some("event") => {
            let event = serde_json::from_value(params()).map_err(|e| {
                format!("the server sent an event the schema does not describe ({e}): {text}")
            })?;
            let _ = tell.send(Incoming::Event(event));
        }
        Some("end") => {
            let end = serde_json::from_value(params()).map_err(|e| {
                format!("the server sent an `end` the schema does not describe ({e}): {text}")
            })?;
            let _ = tell.send(Incoming::End(end));
        }
        Some(other) => {
            return Err(format!(
                "the server sent the notification {other:?}: {text}"
            ));
        }
        None => {
            let id = message.get("id").and_then(Value::as_u64).ok_or_else(|| {
                format!("the server sent a message with no method and no id: {text}")
            })?;
            let reply = match message.get("error") {
                Some(error) => Err(serde_json::from_value(error.clone()).map_err(|e| {
                    format!("the server sent an error the schema does not describe ({e}): {text}")
                })?),
                None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
            };
            let waiter = pending
                .lock()
                .expect("pending is never held across a panic")
                .remove(&id);
            let Some(waiter) = waiter else {
                return Err(format!(
                    "the server answered request {id}, which no call is waiting for: {text}"
                ));
            };
            let _ = waiter.send(reply);
        }
    }
    Ok(())
}

impl Link {
    /// Make a call and wait for its result.
    pub async fn call<P: Call>(&self, params: &P) -> Result<P::Output, CallError> {
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let (waiter, answer) = oneshot::channel();
        self.pending
            .lock()
            .expect("pending is never held across a panic")
            .insert(id, waiter);
        let request = json!({"jsonrpc": "2.0", "id": id, "method": P::METHOD, "params": params});
        if self.out.send(Message::text(request.to_string())).is_err() {
            return Err(CallError::Closed);
        }
        match answer.await {
            Ok(Ok(result)) => serde_json::from_value(result)
                .map_err(|e| CallError::Unreadable(format!("{}: {e}", P::METHOD))),
            Ok(Err(error)) => Err(CallError::Server(error)),
            Err(_) => Err(CallError::Closed),
        }
    }
}
