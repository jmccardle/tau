//! τ store (D12): one session per directory, holding an append-only record
//! log (`log`) and the scratchpad file (`scratchpad`). The only crate that
//! touches the filesystem.
//!
//! Both files start with an 8-byte magic and a little-endian `u32` version.
//! A record on disk is its payload's length (`u32` LE), a CRC-32 of the
//! length and the payload (`u32` LE), then the payload: the `Record` (or, in
//! the scratchpad file, the `Scratchpad`) as JSON.
//!
//! The store also keeps each node's offset in the log, so a body evicted
//! from memory (`Store::evict`) can be read back (`Store::load`) (D14).
//! Open streams the log once, reading each node without its body unless
//! it is a `Config`, then reads back only the bodies on the cursors' walks.

use std::fmt;
use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde::de::{DeserializeOwned, IgnoredAny};
use serde::{Deserialize, Serialize};
use tau_core::tree::{CursorId, ItemId, Move, NodeId, Record, Replay, Scratchpad, Session};

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
    /// A scratchpad file that fails its checksum or decode (the file is
    /// replaced by rename, so this is damage, never a crash), or a log
    /// record that does not read back as the node `load` asked for.
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
            Self::Corrupt(p) => write!(f, "damaged session file: {}", p.display()),
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
    /// Bytes of the log on disk, all synced.
    len: u64,
    /// Per node, by id: where its `Write` record starts in the log, counting
    /// records still in `pending`.
    offsets: Vec<u64>,
    failed: bool,
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
            .read(true)
            .append(true)
            .create(true)
            .open(&tmp)?;
        lease(&log)?;
        log.set_len(0)?;
        let mut store = Self {
            dir: dir.into(),
            log,
            pending: header(LOG_MAGIC),
            len: 0,
            offsets: vec![],
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
    /// Only the bodies on the cursors' walks and `Config`s are read into
    /// memory; the rest are stored, as after `evict`.
    ///
    /// # Errors
    ///
    /// `Leased` if another store holds the lease; `NotASession` for bad magic
    /// or a log with no `Sync`; `Version`; `Corrupt` for a damaged scratchpad
    /// file or a body on a cursor's walk that does not read back; `Core` for
    /// records that do not replay; `Io`.
    pub fn open(dir: &Path) -> Result<(Self, Session), Error> {
        let path = dir.join(LOG);
        let mut log = OpenOptions::new().read(true).append(true).open(&path)?;
        lease(&log)?;
        let size = log.metadata()?.len();
        let mut head = vec![];
        (&log).take(HEADER as u64).read_to_end(&mut head)?;
        check_header(&head, LOG_MAGIC, &path)?;
        let scanned = scan(BufReader::new(&log), size)?;
        if scanned.replays.is_empty() {
            return Err(Error::NotASession(path));
        }
        let end = scanned.kept;
        if end < size {
            let mut tail = vec![];
            log.seek(SeekFrom::Start(end))?;
            log.read_to_end(&mut tail)?;
            let aside = dir.join(format!("dropped-{end}"));
            File::create(&aside)?.write_all(&tail)?;
            File::open(&aside)?.sync_all()?;
            log.set_len(end)?;
            log.sync_all()?;
        }
        let pad = read_pad(&dir.join(PAD))?;
        let mut session = Session::load(scanned.replays, pad).map_err(Error::Core)?;
        let mut store = Self {
            dir: dir.into(),
            log,
            pending: vec![],
            len: end,
            offsets: scanned.offsets,
            failed: false,
            dropped: size - end,
        };
        let cursors = (0..).map_while(|c: CursorId| session.at(c).ok());
        for tip in cursors.collect::<Vec<_>>() {
            if let Err(tau_core::tree::Error::Stored(ids)) = session.resident(tip) {
                store.load(&mut session, &ids)?;
            }
        }
        Ok((store, session))
    }

    /// Takes `session`'s queued records. Everything up to the last queued
    /// `Sync` is written with one log sync, then the scratchpad file is
    /// rewritten from the newest pad those `Sync`s carry, if any (D12): one
    /// sync per call, however many `Sync`s it covers. Records after the last
    /// `Sync` stay buffered.
    ///
    /// # Errors
    ///
    /// `Failed` if an earlier write failed; `Io`, after which every later
    /// call is `Failed`.
    pub fn save(&mut self, session: &mut Session) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Failed);
        }
        // Through the last `Sync`: its end in `pending`, and the newest pad.
        let mut through = None;
        for r in session.outbox.drain(..) {
            let Record::Sync { pad } = r else {
                if let Record::Write { node, .. } = &r {
                    debug_assert_eq!(node.id, self.offsets.len() as NodeId, "writes in id order");
                    self.offsets.push(self.len + self.pending.len() as u64);
                }
                encode(&r, &mut self.pending);
                continue;
            };
            encode(&Record::Sync { pad: None }, &mut self.pending);
            let newest = pad.or(through.and_then(|(_, p)| p));
            through = Some((self.pending.len(), newest));
        }
        let Some((end, pad)) = through else {
            return Ok(());
        };
        self.failed = true;
        self.log.write_all(&self.pending[..end])?;
        self.log.sync_data()?;
        self.len += end as u64;
        self.pending.drain(..end);
        if let Some(pad) = pad {
            self.write_pad(&pad)?;
        }
        self.failed = false;
        Ok(())
    }

    /// Drop from memory every body the log holds synced that is on no
    /// cursor's walk and is not `Config` (`Session::evict`). Returns how many.
    pub fn evict(&self, session: &mut Session) -> usize {
        let durable = self.offsets.partition_point(|&o| o < self.len);
        session.evict(durable as NodeId)
    }

    /// Read the stored bodies of `ids` back from the log into `session`, as
    /// asked by `tree::Error::Stored`. Bodies in memory are skipped.
    ///
    /// # Errors
    ///
    /// `Core(NoSuchNode)` for an id this store never synced; `Corrupt` for a
    /// record that does not read back as that node; `Io`.
    ///
    /// # Panics
    ///
    /// Never: offsets and lengths are far below `i64::MAX`.
    pub fn load(&mut self, session: &mut Session, ids: &[NodeId]) -> Result<(), Error> {
        let mut wanted = vec![];
        for &id in ids.iter().filter(|&&id| session.node(id).is_err()) {
            let at = usize::try_from(id)
                .ok()
                .and_then(|i| self.offsets.get(i))
                .copied()
                .filter(|&o| o < self.len)
                .ok_or(Error::Core(tau_core::tree::Error::NoSuchNode(id)))?;
            wanted.push((at, id));
        }
        // In log order through one buffer: a line's bodies are mostly
        // written in order, so most reads skip forward within it.
        wanted.sort_unstable();
        let mut log = BufReader::with_capacity(1 << 16, &self.log);
        let mut pos = log.seek(SeekFrom::Start(wanted.first().map_or(0, |w| w.0)))?;
        let mut buf = vec![];
        for (at, id) in wanted {
            log.seek_relative(i64::try_from(at).unwrap() - i64::try_from(pos).unwrap())?;
            buf.resize(8, 0);
            log.read_exact(&mut buf)?;
            let len = u32::from_le_bytes(buf[..4].try_into().unwrap()) as usize;
            buf.resize(8 + len, 0);
            log.read_exact(&mut buf[8..])?;
            pos = at + buf.len() as u64;
            let restored = match decode::<Record>(&buf, 0) {
                Some((Record::Write { node, .. }, _)) if node.id == id => session.restore(node),
                _ => Err(tau_core::tree::Error::BadRecord(0)),
            };
            restored.map_err(|_| Error::Corrupt(self.dir.join(LOG)))?;
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

/// What open reads from the log.
struct Scanned {
    /// The records up to the last `Sync` before the first bad record; each
    /// `Write` but a `Config` without its body.
    replays: Vec<Replay>,
    /// Where each of their `Write`s starts.
    offsets: Vec<u64>,
    /// Where the last of them ends.
    kept: u64,
}

/// The log after its header, one record at a time, to `size`. A record cut
/// short, failing its checksum, or not decoding ends the scan.
fn scan(mut log: impl Read, size: u64) -> io::Result<Scanned> {
    let (mut replays, mut batch) = (vec![], vec![]);
    let (mut offsets, mut writes) = (vec![], vec![]);
    let (mut at, mut kept) = (HEADER as u64, HEADER as u64);
    let mut payload = vec![];
    while at + 8 <= size {
        let mut head = [0; 8];
        log.read_exact(&mut head)?;
        let [l0, l1, l2, l3, c0, c1, c2, c3] = head;
        let len = u64::from(u32::from_le_bytes([l0, l1, l2, l3]));
        if at + 8 + len > size {
            break;
        }
        payload.resize(usize::try_from(len).expect("a record under 4 GiB"), 0);
        log.read_exact(&mut payload)?;
        if checksum(&head[..4], &payload) != u32::from_le_bytes([c0, c1, c2, c3]) {
            break;
        }
        let Some(record) = light(&payload) else {
            break;
        };
        let sync = matches!(record, Replay::Record(Record::Sync { .. }));
        if matches!(
            record,
            Replay::Stored { .. } | Replay::Record(Record::Write { .. })
        ) {
            writes.push(at);
        }
        batch.push(record);
        at += 8 + len;
        if sync {
            replays.append(&mut batch);
            offsets.append(&mut writes);
            kept = at;
        }
    }
    Ok(Scanned {
        replays,
        offsets,
        kept,
    })
}

/// A `Record` as open reads it: a `Write` without its body.
#[derive(Deserialize)]
enum Light {
    Write { node: LightNode, mv: Option<Move> },
}

/// A node's id, parent, and delivers, and which kind of body it has.
#[derive(Deserialize)]
struct LightNode {
    id: NodeId,
    parent: Option<NodeId>,
    body: LightBody,
    delivers: Vec<ItemId>,
}

/// A body's kind, its content skipped unbuilt. Mirrors `Body`; a body that
/// does not match is read whole instead.
#[derive(Deserialize)]
enum LightBody {
    System(IgnoredAny),
    User(IgnoredAny),
    Assistant(IgnoredAny),
    Results(IgnoredAny),
    Config(IgnoredAny),
    Custom(IgnoredAny),
}

/// `payload` as open replays it: a `Write` of anything but a `Config`
/// without its body; anything else, or anything `Light` does not match,
/// whole. `None` if it does not decode.
fn light(payload: &[u8]) -> Option<Replay> {
    match serde_json::from_slice(payload) {
        Ok(Light::Write { node, mv }) if !matches!(node.body, LightBody::Config(_)) => {
            Some(Replay::Stored {
                id: node.id,
                parent: node.parent,
                delivers: node.delivers,
                mv,
            })
        }
        _ => serde_json::from_slice(payload).ok().map(Replay::Record),
    }
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
