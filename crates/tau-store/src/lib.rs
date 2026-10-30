//! τ store (D12): one session per directory, holding an append-only record
//! log (`log`) and the scratchpad file (`scratchpad`). The only crate that
//! touches the filesystem.
//!
//! Both files start with an 8-byte magic and a little-endian `u32` version.
//! A record on disk is its payload's length (`u32` LE), a CRC-32 of the
//! length and the payload (`u32` LE), then the payload: the `Record` (or, in
//! the scratchpad file, the `Scratchpad`) as JSON.

use std::fmt;
use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde::de::DeserializeOwned;
use tau_core::tree::{Record, Scratchpad, Session};

const LOG: &str = "log";
const PAD: &str = "scratchpad";
const LOG_MAGIC: [u8; 8] = *b"tau-log\0";
const PAD_MAGIC: [u8; 8] = *b"tau-pad\0";
/// The file version this build writes and reads.
pub const VERSION: u32 = 1;
/// Magic plus version.
pub const HEADER: usize = 12;

#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    /// Another store holds the session's lease.
    Leased,
    /// `create` where a session already is.
    Exists,
    /// Not a τ log or scratchpad file (bad magic), or a log with no `Sync`.
    NotASession(PathBuf),
    /// A file of a version this build does not read.
    Version(u32),
    /// A scratchpad file that fails its checksum or decode. The file is
    /// replaced by rename, so this is damage, never a crash.
    Corrupt(PathBuf),
    /// A log record whose bytes are intact (length and checksum agree) but
    /// do not decode: a format change, a bug, or damage that kept the
    /// checksum. Never a crash, so never cut. `record` counts from 0.
    Undecodable {
        path: PathBuf,
        record: usize,
        offset: usize,
        /// The decoder's reason.
        reason: String,
    },
    /// The log's records do not replay (`tree::Error::BadRecord`).
    Core(tau_core::tree::Error),
    /// A record whose encoding is 4 GiB or more, which the format's `u32`
    /// length cannot hold. Nothing of the failing `save` reached the log.
    TooLarge(usize),
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(e) => write!(f, "{e}"),
            Self::Leased => f.write_str("another store holds this session's lease"),
            Self::Exists => f.write_str("a session already exists here"),
            Self::NotASession(p) => write!(f, "not a τ session file: {}", p.display()),
            Self::Version(v) => write!(f, "session file version {v}; this build reads {VERSION}"),
            Self::Corrupt(p) => write!(f, "damaged scratchpad file: {}", p.display()),
            Self::Undecodable {
                path,
                record,
                offset,
                reason,
            } => write!(
                f,
                "record {record} (byte {offset}) of {} is intact but does not decode: {reason}",
                path.display()
            ),
            Self::Core(e) => write!(f, "the log does not replay: {e:?}"),
            Self::TooLarge(n) => {
                write!(f, "a record of {n} bytes does not fit the log's u32 length")
            }
        }
    }
}

impl std::error::Error for Error {}

/// One open session on disk. Holds the lease (an exclusive `flock` on the
/// log) while it lives; only the holder writes. A failed `save` consumes it:
/// what reached the log is unknown, so the session is reopened from disk, as
/// after a crash.
#[derive(Debug)]
pub struct Store {
    dir: PathBuf,
    log: File,
    /// Encoded records since the last `Sync`, not yet written.
    pending: Vec<u8>,
    /// Bytes cut from the log's tail by `open`: a torn or unsynced tail. Kept
    /// in the session directory as `dropped-<offset>`.
    pub dropped: u64,
}

impl Store {
    /// A new session in `dir` (created if missing), holding `session`'s
    /// records so far, synced.
    ///
    /// # Errors
    ///
    /// `Exists` if `dir` already holds a log; `Leased` if another store holds
    /// the new log's lease; `Io`; whatever `save` returns.
    pub fn create(dir: &Path, session: &mut Session) -> Result<Self, Error> {
        fs::create_dir_all(dir)?;
        // Written whole under a private name, then linked into place: a crash
        // never leaves a log without its first `Sync`, and the link refuses
        // to replace one. The lease is on the file, so it holds through the link.
        let tmp = dir.join(format!("{LOG}.new-{}", std::process::id()));
        let log = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)?;
        lease(&log)?;
        let store = Self {
            dir: dir.into(),
            log,
            pending: header(LOG_MAGIC),
            dropped: 0,
        };
        session.sync();
        let store = store.save(session)?;
        let linked = fs::hard_link(&tmp, dir.join(LOG));
        fs::remove_file(&tmp)?;
        match linked {
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => return Err(Error::Exists),
            r => r?,
        }
        sync_dir(dir)?;
        Ok(store)
    }

    /// Takes the lease and loads the session (I7): the log's records up to
    /// its last `Sync`, and the scratchpad file. Anything after that `Sync`
    /// (a torn tail, records never synced, damage) is cut and kept aside.
    ///
    /// # Errors
    ///
    /// `Leased` if another store holds the lease; `NotASession` for bad magic
    /// or a log with no `Sync`; `Version`; `Corrupt` for a damaged scratchpad
    /// file; `Undecodable` for an intact log record that does not decode; `Core` for records that do not replay; `Io`.
    pub fn open(dir: &Path) -> Result<(Self, Session), Error> {
        let path = dir.join(LOG);
        let mut log = OpenOptions::new().read(true).append(true).open(&path)?;
        lease(&log)?;
        let mut bytes = vec![];
        log.read_to_end(&mut bytes)?;
        check_header(&bytes, LOG_MAGIC, &path)?;
        let (records, kept) = scan(&bytes[HEADER..], &path)?;
        if records.is_empty() {
            return Err(Error::NotASession(path));
        }
        let end = HEADER + kept;
        cut_tail(dir, &log, &bytes, end)?;
        let pad = read_pad(&dir.join(PAD))?;
        let session = tau_core::tree::load(records, pad).map_err(Error::Core)?;
        let store = Self {
            dir: dir.into(),
            log,
            pending: vec![],
            dropped: (bytes.len() - end) as u64,
        };
        Ok((store, session))
    }

    /// Takes `session`'s queued records. Everything up to the last queued
    /// `Sync` is written with one log sync, then the scratchpad file is
    /// rewritten from the newest pad those `Sync`s carry, if any (D12): one
    /// sync per call, however many `Sync`s it covers. Records after the last
    /// `Sync` stay buffered. Returns the store to save with next.
    ///
    /// # Errors
    ///
    /// `Io`. The store is gone (its lease released): open the session again,
    /// which cuts anything after the log's last good `Sync`.
    pub fn save(mut self, session: &mut Session) -> Result<Self, Error> {
        let _span = tracing::info_span!("store.save").entered();
        // Through the last `Sync`: its end in `pending`, and the newest pad.
        let mut through = None;
        for r in session.outbox.drain(..) {
            let Record::Sync { pad } = r else {
                encode(&r, &mut self.pending)?;
                continue;
            };
            encode(&Record::Sync { pad: None }, &mut self.pending)?;
            let newest = pad.or(through.and_then(|(_, p)| p));
            through = Some((self.pending.len(), newest));
        }
        let Some((end, pad)) = through else {
            return Ok(self);
        };
        {
            let _span = tracing::info_span!("store.append", bytes = end).entered();
            self.log.write_all(&self.pending[..end])?;
        }
        {
            let _span = tracing::info_span!("store.fsync_log").entered();
            self.log.sync_data()?;
        }
        self.pending.drain(..end);
        if let Some(pad) = pad {
            self.write_pad(&pad)?;
        }
        Ok(self)
    }

    fn write_pad(&self, pad: &Scratchpad) -> Result<(), Error> {
        let _span = tracing::info_span!("store.write_pad").entered();
        let tmp = self.dir.join(format!("{PAD}.new"));
        let mut bytes = header(PAD_MAGIC);
        encode(pad, &mut bytes)?;
        let mut f = File::create(&tmp)?;
        f.write_all(&bytes)?;
        {
            let _span = tracing::info_span!("store.fsync_pad").entered();
            f.sync_data()?;
        }
        fs::rename(&tmp, self.dir.join(PAD))?;
        let _span = tracing::info_span!("store.fsync_dir").entered();
        sync_dir(&self.dir)
    }
}

/// Keeps `bytes[end..]` aside as `dropped-<end>`, then cuts the log to `end`.
fn cut_tail(dir: &Path, log: &File, bytes: &[u8], end: usize) -> Result<(), Error> {
    if end < bytes.len() {
        let aside = dir.join(format!("dropped-{end}"));
        File::create(&aside)?.write_all(&bytes[end..])?;
        File::open(&aside)?.sync_all()?;
        log.set_len(end as u64)?;
        log.sync_all()?;
    }
    Ok(())
}

fn lease(f: &File) -> Result<(), Error> {
    f.try_lock().map_err(|e| match e {
        TryLockError::WouldBlock => Error::Leased,
        TryLockError::Error(e) => Error::Io(e),
    })
}

fn sync_dir(dir: &Path) -> Result<(), Error> {
    File::open(dir)?.sync_all()?;
    Ok(())
}

fn header(magic: [u8; 8]) -> Vec<u8> {
    let mut h = magic.to_vec();
    h.extend_from_slice(&VERSION.to_le_bytes());
    h
}

fn check_header(bytes: &[u8], magic: [u8; 8], path: &Path) -> Result<(), Error> {
    if bytes.len() < HEADER || bytes[..8] != magic {
        return Err(Error::NotASession(path.into()));
    }
    let version = u32::from_le_bytes(bytes[8..HEADER].try_into().unwrap());
    if version != VERSION {
        return Err(Error::Version(version));
    }
    Ok(())
}

fn checksum(len: &[u8], payload: &[u8]) -> u32 {
    let mut h = crc32fast::Hasher::new();
    h.update(len);
    h.update(payload);
    h.finalize()
}

/// A payload length as the log stores it, or `TooLarge`.
fn record_len(len: usize) -> Result<u32, Error> {
    u32::try_from(len).map_err(|_| Error::TooLarge(len))
}

fn encode(value: &impl Serialize, out: &mut Vec<u8>) -> Result<(), Error> {
    let payload = serde_json::to_vec(value).expect("core types serialize");
    let len = record_len(payload.len())?.to_le_bytes();
    out.extend_from_slice(&len);
    out.extend_from_slice(&checksum(&len, &payload).to_le_bytes());
    out.extend_from_slice(&payload);
    Ok(())
}

/// What `decode` found at a position.
enum Decoded<T> {
    /// A whole record and where the next one starts.
    Record(T, usize),
    /// Cut short or failing its checksum: a torn tail, or damage.
    Torn,
    /// Length and checksum agree, so the bytes are as written, but they do
    /// not decode.
    Undecodable(String),
}

fn decode<T: DeserializeOwned>(bytes: &[u8], at: usize) -> Decoded<T> {
    let Some(head) = at.checked_add(8).and_then(|e| bytes.get(at..e)) else {
        return Decoded::Torn;
    };
    let len = u32::from_le_bytes(head[..4].try_into().unwrap()) as usize;
    let crc = u32::from_le_bytes(head[4..].try_into().unwrap());
    let Some(end) = (at + 8).checked_add(len) else {
        return Decoded::Torn;
    };
    let Some(payload) = bytes.get(at + 8..end) else {
        return Decoded::Torn;
    };
    if checksum(&head[..4], payload) != crc {
        return Decoded::Torn;
    }
    match serde_json::from_slice(payload) {
        Ok(v) => Decoded::Record(v, end),
        Err(e) => Decoded::Undecodable(e.to_string()),
    }
}

/// The records up to the last `Sync` before the first torn record, and the
/// bytes they span. A record that is whole but does not decode is an error,
/// wherever it is: only a short or checksum-failing record ends the scan.
fn scan(bytes: &[u8], path: &Path) -> Result<(Vec<Record>, usize), Error> {
    let (mut records, mut batch) = (vec![], vec![]);
    let (mut at, mut kept) = (0, 0);
    for n in 0.. {
        match decode::<Record>(bytes, at) {
            Decoded::Record(r, next) => {
                let sync = matches!(r, Record::Sync { .. });
                batch.push(r);
                at = next;
                if sync {
                    records.append(&mut batch);
                    kept = at;
                }
            }
            Decoded::Torn => break,
            Decoded::Undecodable(reason) => {
                return Err(Error::Undecodable {
                    path: path.into(),
                    record: n,
                    offset: HEADER + at,
                    reason,
                });
            }
        }
    }
    Ok((records, kept))
}

fn read_pad(path: &Path) -> Result<Scratchpad, Error> {
    let bytes = match fs::read(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Scratchpad::default()),
        r => r?,
    };
    check_header(&bytes, PAD_MAGIC, path)?;
    let body = &bytes[HEADER..];
    match decode(body, 0) {
        Decoded::Record(pad, end) if end == body.len() => Ok(pad),
        _ => Err(Error::Corrupt(path.into())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_record_of_4_gib_is_refused() {
        assert_eq!(record_len(u32::MAX as usize).unwrap(), u32::MAX);
        let too_big = u32::MAX as usize + 1;
        assert!(matches!(record_len(too_big), Err(Error::TooLarge(n)) if n == too_big));
    }
}
