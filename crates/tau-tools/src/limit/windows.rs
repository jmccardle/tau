//! The overflow directory on Windows: the check `unix.rs` makes with `fstat`, made
//! with the directory's security descriptor.
//!
//! The directory is opened as a handle (not following a reparse point: a symlink or
//! junction is refused) and held without `FILE_SHARE_DELETE`, so it cannot be renamed or
//! replaced while files are made in it by path. It must be owned by this process's
//! user (or by the token's default owner, which is `Administrators` for an elevated
//! administrator), and every entry of its access list that grants anything must name
//! the user, that owner, SYSTEM or Administrators: the Windows reading of `0700`
//! (SYSTEM and Administrators can read anything, as root can). An entry of a kind
//! this check cannot judge refuses the directory. A new directory inherits its
//! parent's access list; the user's temp directory, the default's parent, grants only
//! those.

use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::ptr;
use std::time::SystemTime;

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, LocalFree};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, GetSecurityInfo, SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::{
    ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, ACL_SIZE_INFORMATION, AclSizeInformation,
    DACL_SECURITY_INFORMATION, EqualSid, GetAce, GetAclInformation, GetTokenInformation,
    INHERIT_ONLY_ACE, IsWellKnownSid, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
    TOKEN_OWNER, TOKEN_QUERY, TOKEN_USER, TokenOwner, TokenUser, WinBuiltinAdministratorsSid,
    WinLocalSystemSid,
};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_SHARE_READ, FILE_SHARE_WRITE,
    READ_CONTROL,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

use super::{KEEP_FILES, is_overflow_name};

const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;
const ACCESS_DENIED_ACE_TYPE: u8 = 1;

/// The overflow directory, open and checked.
#[derive(Debug)]
pub(super) struct PrivateDir {
    path: PathBuf,
    /// Held so the directory stays where it was checked.
    _handle: File,
}

impl PrivateDir {
    /// Open `dir` (created if absent), refusing a reparse point, and check its owner and
    /// access list on the open handle. Then drop what is old in it. The error is the
    /// reason, for the note.
    pub(super) fn open(dir: &Path) -> Result<Self, String> {
        let shown = dir.display();
        std::fs::create_dir_all(dir)
            .map_err(|e| format!("the overflow directory {shown} could not be created: {e}"))?;
        let handle = OpenOptions::new()
            .access_mode(READ_CONTROL | FILE_LIST_DIRECTORY)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(dir)
            .map_err(|e| format!("the overflow directory {shown} cannot be opened: {e}"))?;
        let attributes = handle
            .metadata()
            .map_err(|e| format!("cannot inspect the overflow directory {shown}: {e}"))?
            .file_attributes();
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!("the overflow directory {shown} is a symlink"));
        }
        if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
            return Err(format!("the overflow directory {shown} is not a directory"));
        }
        if let Some(problem) = dir_problem(&handle)
            .map_err(|e| format!("cannot inspect the overflow directory {shown}: {e}"))?
        {
            return Err(format!("the overflow directory {shown} {problem}"));
        }
        remove_old_files(dir);
        Ok(Self {
            path: dir.to_owned(),
            _handle: handle,
        })
    }

    /// A new file `name` in the directory; `None` if the name is taken. The error is the
    /// reason.
    pub(super) fn create(&self, name: &str) -> Result<Option<File>, String> {
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(self.path.join(name))
        {
            Ok(file) => Ok(Some(file)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// Remove the overflow files in `dir` older than [`KEEP_FILES`]. Cleanup: a file that
/// cannot be listed or removed stays, which costs disk and nothing else.
fn remove_old_files(dir: &Path) {
    let Ok(listing) = std::fs::read_dir(dir) else {
        return;
    };
    let now = SystemTime::now();
    for item in listing.flatten() {
        let name = item.file_name();
        if !name
            .to_str()
            .is_some_and(|n| is_overflow_name(n.as_bytes()))
        {
            continue;
        }
        let Ok(meta) = std::fs::symlink_metadata(item.path()) else {
            continue;
        };
        let old = meta
            .modified()
            .ok()
            .and_then(|at| now.duration_since(at).ok())
            .is_some_and(|age| age > KEEP_FILES);
        if meta.file_type().is_file() && old {
            let _ = std::fs::remove_file(item.path());
        }
    }
}

/// Frees what Win32 allocated with `LocalAlloc`.
struct Local(*mut c_void);

impl Drop for Local {
    #[allow(unsafe_code)]
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: the pointer came from an API that says to free it with LocalFree,
            // and it is freed once, here.
            unsafe { LocalFree(self.0) };
        }
    }
}

/// A token handle, closed on drop.
struct Token(HANDLE);

impl Drop for Token {
    #[allow(unsafe_code)]
    fn drop(&mut self) {
        // SAFETY: the handle was opened by OpenProcessToken and is closed once, here.
        unsafe { CloseHandle(self.0) };
    }
}

/// The SIDs this process may own and share the directory with.
struct Ours {
    user: Vec<u64>,
    owner: Vec<u64>,
}

impl Ours {
    fn user(&self) -> PSID {
        // SAFETY of the read: the buffer holds a TOKEN_USER (see `token_info`).
        #[allow(unsafe_code)]
        unsafe {
            (*self.user.as_ptr().cast::<TOKEN_USER>()).User.Sid
        }
    }

    fn owner(&self) -> PSID {
        // SAFETY of the read: the buffer holds a TOKEN_OWNER (see `token_info`).
        #[allow(unsafe_code)]
        unsafe {
            (*self.owner.as_ptr().cast::<TOKEN_OWNER>()).Owner
        }
    }

    /// Whether `sid` is the user, the token's default owner, SYSTEM or Administrators.
    #[allow(unsafe_code)]
    fn may_have(&self, sid: PSID) -> bool {
        // SAFETY: every SID here is valid while `self` and the descriptor it came from
        // live, which outlasts the call.
        unsafe {
            EqualSid(sid, self.user()) != 0
                || EqualSid(sid, self.owner()) != 0
                || IsWellKnownSid(sid, WinLocalSystemSid) != 0
                || IsWellKnownSid(sid, WinBuiltinAdministratorsSid) != 0
        }
    }
}

/// A token information class of this process, in a buffer aligned for the struct it holds.
#[allow(unsafe_code)]
fn token_info(token: &Token, class: i32) -> std::io::Result<Vec<u64>> {
    let mut size = 0_u32;
    // SAFETY: a null buffer of length 0 asks only for the size, written to `size`.
    unsafe { GetTokenInformation(token.0, class, ptr::null_mut(), 0, &raw mut size) };
    let mut buf = vec![0_u64; (size as usize).div_ceil(8)];
    let bytes = u32::try_from(buf.len() * 8).map_err(std::io::Error::other)?;
    // SAFETY: `buf` is `bytes` long and 8-aligned, which suits the struct written.
    let ok = unsafe {
        GetTokenInformation(
            token.0,
            class,
            buf.as_mut_ptr().cast(),
            bytes,
            &raw mut size,
        )
    };
    if ok == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(buf)
}

#[allow(unsafe_code)]
fn ours() -> std::io::Result<Ours> {
    let mut handle: HANDLE = ptr::null_mut();
    // SAFETY: the pseudo-handle of this process needs no closing; the token is closed
    // by `Token`.
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw mut handle) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let token = Token(handle);
    Ok(Ours {
        user: token_info(&token, TokenUser)?,
        owner: token_info(&token, TokenOwner)?,
    })
}

/// `sid` as text (`S-1-5-...`), for a message.
#[allow(unsafe_code)]
fn sid_text(sid: PSID) -> String {
    let mut text = ptr::null_mut();
    // SAFETY: `sid` is valid; the string is allocated by the call and freed by `Local`.
    if unsafe { ConvertSidToStringSidW(sid, &raw mut text) } == 0 {
        return "an unknown account".to_owned();
    }
    let _free = Local(text.cast());
    // SAFETY: the call wrote a NUL-terminated UTF-16 string at `text`.
    let len = (0..usize::MAX)
        .take_while(|&i| unsafe { *text.add(i) } != 0)
        .count();
    // SAFETY: `len` UTF-16 units precede the NUL.
    String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(text, len) })
}

/// What is wrong with the owner or access list of the open directory, if anything.
#[allow(unsafe_code)]
fn dir_problem(dir: &File) -> std::io::Result<Option<String>> {
    let ours = ours()?;
    let mut owner: PSID = ptr::null_mut();
    let mut dacl: *mut ACL = ptr::null_mut();
    let mut descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
    // SAFETY: the handle is open with READ_CONTROL; the out pointers point into the
    // descriptor, which is freed by `Local` after the last use of them below.
    let status = unsafe {
        GetSecurityInfo(
            dir.as_raw_handle(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &raw mut owner,
            ptr::null_mut(),
            &raw mut dacl,
            ptr::null_mut(),
            &raw mut descriptor,
        )
    };
    if status != 0 {
        return Err(std::io::Error::from_raw_os_error(
            i32::try_from(status).unwrap_or(i32::MAX),
        ));
    }
    let _free = Local(descriptor);
    // SAFETY: `owner` points into the live descriptor.
    if unsafe { EqualSid(owner, ours.user()) } == 0 && unsafe { EqualSid(owner, ours.owner()) } == 0
    {
        return Ok(Some(format!(
            "is owned by {}, not by this process",
            sid_text(owner)
        )));
    }
    if dacl.is_null() {
        return Ok(Some(
            "has no access list (everyone may use it); it must be accessible only by its owner"
                .to_owned(),
        ));
    }
    let mut size = ACL_SIZE_INFORMATION::default();
    let bytes = u32::try_from(size_of::<ACL_SIZE_INFORMATION>()).unwrap_or(u32::MAX);
    // SAFETY: `dacl` points into the live descriptor; `size` is the struct asked for.
    if unsafe { GetAclInformation(dacl, (&raw mut size).cast(), bytes, AclSizeInformation) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    for index in 0..size.AceCount {
        let mut ace: *mut c_void = ptr::null_mut();
        // SAFETY: `index` is below the entry count of the live list.
        if unsafe { GetAce(dacl, index, &raw mut ace) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
        // SAFETY: every entry starts with an ACE_HEADER.
        let header = unsafe { *ace.cast::<ACE_HEADER>() };
        if u32::from(header.AceFlags) & INHERIT_ONLY_ACE != 0
            || header.AceType == ACCESS_DENIED_ACE_TYPE
        {
            continue; // grants nothing on the directory itself
        }
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE {
            return Ok(Some(format!(
                "has an access entry of type {} that tau cannot judge; it must be accessible \
                 only by its owner",
                header.AceType
            )));
        }
        // SAFETY: an ACCESS_ALLOWED_ACE holds its SID from `SidStart` on.
        let sid: PSID = unsafe { (&raw mut (*ace.cast::<ACCESS_ALLOWED_ACE>()).SidStart).cast() };
        if !ours.may_have(sid) {
            return Ok(Some(format!(
                "grants access to {}; it must be accessible only by its owner",
                sid_text(sid)
            )));
        }
    }
    Ok(None)
}
