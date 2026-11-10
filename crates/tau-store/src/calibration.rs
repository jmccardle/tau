//! The calibration store (D26 section 6.5): one JSON file per model under a
//! directory (`$TAU_HOME/calibration/`), each a
//! [`tau_core::calibration::Record`]. Only the `tokens` built-in opens one,
//! and nothing here touches the disk until it is asked to read or to
//! observe.
//!
//! An observation is folded into the record that is on disk when the
//! store holds the file's lock: lock, read, fold, write to a new file, rename
//! it over the old one. So two processes that observe the same model lose
//! nothing of each other's, and a reader (which takes no lock) sees the old
//! record or the new one, never part of one.
//!
//! The lock is the standard library's advisory file lock on `<name>.lock`,
//! which exists on Unix and on Windows; the rename replaces the file on
//! both. Only the files' mode (600) is Unix's alone.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};

use tau_core::calibration::{Event, Key, Observation, Record, VERSION, fold};

const RECORD: &str = "json";
const LOCK: &str = "lock";

#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    /// A record written by a newer τ: left alone, and not used.
    Newer {
        path: PathBuf,
        version: u64,
    },
    /// A file that is not a calibration record this build reads (not JSON,
    /// not the record's shape, or another model's). Never overwritten:
    /// removing it is the user's to do.
    Damaged {
        path: PathBuf,
        reason: String,
    },
    /// A record name that is not a relative path of plain names ending in
    /// `.json`.
    BadName(String),
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
            Self::Newer { path, version } => write!(
                f,
                "{} is a calibration record of version {version}, from a newer tau; this build \
                 reads {VERSION} and leaves it alone",
                path.display()
            ),
            Self::Damaged { path, reason } => write!(
                f,
                "{} is not a calibration record this build reads: {reason}",
                path.display()
            ),
            Self::BadName(name) => write!(f, "{name:?} is not the name of a calibration record"),
        }
    }
}

impl std::error::Error for Error {}

/// The records under one directory.
#[derive(Clone, Debug)]
pub struct CalibrationStore {
    dir: PathBuf,
}

impl CalibrationStore {
    /// The store of `dir`. Nothing is read, written or created.
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The directory the records are under.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn path(&self, name: &str) -> Result<PathBuf, Error> {
        let given = Path::new(name);
        let plain = given
            .components()
            .all(|c| matches!(c, Component::Normal(_)));
        if !plain || given.extension().is_none_or(|e| e != RECORD) {
            return Err(Error::BadName(name.into()));
        }
        Ok(self.dir.join(given))
    }

    /// The record named `name`, or `None` if there is none. Takes no lock.
    ///
    /// # Errors
    ///
    /// `Newer`, `Damaged`, `BadName`, `Io`.
    pub fn read(&self, name: &str) -> Result<Option<Record>, Error> {
        read_file(&self.path(name)?)
    }

    /// Folds one observation into the record named `name` (a new record of
    /// `key` if there is none) under the file's lock, writes it, and returns
    /// it with what the fold found. Creates the directory if it is missing.
    ///
    /// # Errors
    ///
    /// `Newer` and `Damaged` (the file is left as it is), `BadName`, `Io`.
    pub fn observe(
        &self,
        name: &str,
        key: &Key,
        observation: &Observation,
    ) -> Result<(Record, Vec<Event>), Error> {
        let path = self.path(name)?;
        let _held = lock(&path)?;
        let record = match read_file(&path)? {
            Some(record) if record.key == *key => record,
            Some(record) => {
                return Err(Error::Damaged {
                    path,
                    reason: format!("it is the record of {:?}", record.key),
                });
            }
            None => Record::new(key.clone()),
        };
        let (record, events) = fold(record, observation);
        write_file(&path, &record)?;
        Ok((record, events))
    }

    /// Removes the record named `name`, whatever it holds; whether there was
    /// one.
    ///
    /// # Errors
    ///
    /// `BadName`, `Io`.
    pub fn remove(&self, name: &str) -> Result<bool, Error> {
        let path = self.path(name)?;
        if !path.exists() {
            return Ok(false);
        }
        let _held = lock(&path)?;
        match fs::remove_file(&path) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    /// The names of the records in the store, sorted: `<directory>/<file>`
    /// for each record file one directory down. Empty if the store's
    /// directory does not exist.
    ///
    /// # Errors
    ///
    /// `Io`.
    pub fn names(&self) -> Result<Vec<String>, Error> {
        let mut names = vec![];
        for group in paths_in(&self.dir)? {
            if !group.is_dir() {
                continue;
            }
            for file in paths_in(&group)? {
                if file.extension().is_some_and(|e| e == RECORD) {
                    let parts = [group.file_name(), file.file_name()];
                    let [Some(a), Some(b)] = parts else { continue };
                    names.push(format!("{}/{}", a.to_string_lossy(), b.to_string_lossy()));
                }
            }
        }
        names.sort();
        Ok(names)
    }
}

fn paths_in(dir: &Path) -> Result<Vec<PathBuf>, Error> {
    let listed = match fs::read_dir(dir) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        r => r?,
    };
    let mut paths = vec![];
    for item in listed {
        paths.push(item?.path());
    }
    Ok(paths)
}

/// Files only their owner reads: a record names a server's address.
fn private(options: &mut OpenOptions) -> &mut OpenOptions {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

/// The exclusive lock on `<path>.lock`, held until the file is dropped. It
/// waits for another holder.
fn lock(path: &Path) -> Result<File, Error> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    let file = private(&mut options).open(path.with_extension(LOCK))?;
    file.lock()?;
    Ok(file)
}

fn read_file(path: &Path) -> Result<Option<Record>, Error> {
    let bytes = match fs::read(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        r => r?,
    };
    let damaged = |reason: String| Error::Damaged {
        path: path.into(),
        reason,
    };
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|e| damaged(e.to_string()))?;
    let version = value.get("version").and_then(serde_json::Value::as_u64);
    match version {
        Some(v) if v > u64::from(VERSION) => {
            return Err(Error::Newer {
                path: path.into(),
                version: v,
            });
        }
        Some(v) if v == u64::from(VERSION) => {}
        _ => return Err(damaged("it has no version this build knows".into())),
    }
    let record: Record = serde_json::from_value(value).map_err(|e| damaged(e.to_string()))?;
    if !record.well_formed() {
        return Err(damaged("its sums are not the shape of this build's".into()));
    }
    Ok(Some(record))
}

/// Writes the record beside `path` and renames it into place: a reader
/// never sees part of a record, and a crash leaves the old one.
fn write_file(path: &Path, record: &Record) -> Result<(), Error> {
    let tmp = path.with_extension(format!("new-{}", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    let mut file = private(&mut options).open(&tmp)?;
    let text = serde_json::to_vec(record).expect("a record serializes");
    file.write_all(&text)?;
    file.sync_data()?;
    drop(file);
    fs::rename(&tmp, path)?;
    Ok(())
}
