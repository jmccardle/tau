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
const LOG_MAGIC: &[u8; 8] = b"tau-log\0";
const PAD_MAGIC: &[u8; 8] = b"tau-pad\0";
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
    /// An earlier write failed; what reached the log is unknown. Open the
    /// session again.
    Failed,
    /// The log's records do not replay (`tree::Error::BadRecord`).
    Core(tau_core::tree::Error),
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
            Self::Failed => f.write_str("an earlier write failed; open the session again"),
            Self::Core(e) => write!(f, "the log does not replay: {e:?}"),
        }
    }
}

impl std::error::Error for Error {}

/// One open session on disk. Holds the lease (an exclusive `flock` on the
/// log) while it lives; only the holder writes.
#[derive(Debug)]
pub struct Store {
    dir: PathBuf,
    log: File,
    /// Encoded records since the last `Sync`, not yet written.
    pending: Vec<u8>,
    failed: bool,
    /// Bytes cut from the log's tail by `open`: a torn or unsynced tail. Kept
    /// in the session directory as `dropped-<offset>`.
    pub dropped: u64,
}

impl Store {
    /// A new session in `dir` (created if missing), holding `session`'s
    /// records so far, synced. `Exists` if `dir` already holds a log.
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
        let mut store = Self {
            dir: dir.into(),
            log,
            pending: header(LOG_MAGIC),
            failed: false,
            dropped: 0,
        };
        session.sync();
        store.save(session)?;
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
    pub fn open(dir: &Path) -> Result<(Self, Session), Error> {
        let path = dir.join(LOG);
        let mut log = OpenOptions::new().read(true).append(true).open(&path)?;
        lease(&log)?;
        let mut bytes = vec![];
        log.read_to_end(&mut bytes)?;
        check_header(&bytes, LOG_MAGIC, &path)?;
        let (records, kept) = scan(&bytes[HEADER..]);
        if records.is_empty() {
            return Err(Error::NotASession(path));
        }
        let end = HEADER + kept;
        if end < bytes.len() {
            let aside = dir.join(format!("dropped-{end}"));
            File::create(&aside)?.write_all(&bytes[end..])?;
            File::open(&aside)?.sync_all()?;
            log.set_len(end as u64)?;
            log.sync_all()?;
        }
        let pad = read_pad(&dir.join(PAD))?;
        let session = Session::load(records, pad).map_err(Error::Core)?;
        let store = Self {
            dir: dir.into(),
            log,
            pending: vec![],
            failed: false,
            dropped: (bytes.len() - end) as u64,
        };
        Ok((store, session))
    }

    /// Takes `session`'s queued records. They are buffered up to each
    /// `Sync`; at a `Sync` the log is written and synced, then the scratchpad
    /// file rewritten if the `Sync` carries it (D12). Records after the last
    /// `Sync` stay buffered.
    pub fn save(&mut self, session: &mut Session) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Failed);
        }
        for r in session.outbox.drain(..) {
            let Record::Sync { pad } = r else {
                encode(&r, &mut self.pending);
                continue;
            };
            encode(&Record::Sync { pad: None }, &mut self.pending);
            self.failed = true;
            self.log.write_all(&self.pending)?;
            self.log.sync_data()?;
            self.pending.clear();
            if let Some(pad) = pad {
                self.write_pad(&pad)?;
            }
            self.failed = false;
        }
        Ok(())
    }

    fn write_pad(&self, pad: &Scratchpad) -> Result<(), Error> {
        let tmp = self.dir.join(format!("{PAD}.new"));
        let mut bytes = header(PAD_MAGIC);
        encode(pad, &mut bytes);
        let mut f = File::create(&tmp)?;
        f.write_all(&bytes)?;
        f.sync_data()?;
        fs::rename(&tmp, self.dir.join(PAD))?;
        sync_dir(&self.dir)
    }
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

fn header(magic: &[u8; 8]) -> Vec<u8> {
    let mut h = magic.to_vec();
    h.extend_from_slice(&VERSION.to_le_bytes());
    h
}

fn check_header(bytes: &[u8], magic: &[u8; 8], path: &Path) -> Result<(), Error> {
    if bytes.len() < HEADER || &bytes[..8] != magic {
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

fn encode(value: &impl Serialize, out: &mut Vec<u8>) {
    let payload = serde_json::to_vec(value).expect("core types serialize");
    let len = u32::try_from(payload.len())
        .expect("a record under 4 GiB")
        .to_le_bytes();
    out.extend_from_slice(&len);
    out.extend_from_slice(&checksum(&len, &payload).to_le_bytes());
    out.extend_from_slice(&payload);
}

/// The record at `at` and where the next one starts; `None` if it is cut
/// short, fails its checksum, or does not decode.
fn decode<T: DeserializeOwned>(bytes: &[u8], at: usize) -> Option<(T, usize)> {
    let head = bytes.get(at..at.checked_add(8)?)?;
    let len = u32::from_le_bytes(head[..4].try_into().unwrap()) as usize;
    let crc = u32::from_le_bytes(head[4..].try_into().unwrap());
    let end = (at + 8).checked_add(len)?;
    let payload = bytes.get(at + 8..end)?;
    if checksum(&head[..4], payload) != crc {
        return None;
    }
    Some((serde_json::from_slice(payload).ok()?, end))
}

/// The records up to the last `Sync` before the first bad record, and the
/// bytes they span.
fn scan(bytes: &[u8]) -> (Vec<Record>, usize) {
    let (mut records, mut batch) = (vec![], vec![]);
    let (mut at, mut kept) = (0, 0);
    while let Some((r, next)) = decode::<Record>(bytes, at) {
        let sync = matches!(r, Record::Sync { .. });
        batch.push(r);
        at = next;
        if sync {
            records.append(&mut batch);
            kept = at;
        }
    }
    (records, kept)
}

fn read_pad(path: &Path) -> Result<Scratchpad, Error> {
    let bytes = match fs::read(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Scratchpad::default()),
        r => r?,
    };
    check_header(&bytes, PAD_MAGIC, path)?;
    let body = &bytes[HEADER..];
    match decode(body, 0) {
        Some((pad, end)) if end == body.len() => Ok(pad),
        _ => Err(Error::Corrupt(path.into())),
    }
}
