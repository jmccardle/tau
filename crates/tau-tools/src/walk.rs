//! The directory walk `grep` and `find` share: ripgrep's, in parallel.
//!
//! `.gitignore` applies whether or not the tree is a git repository, as do `.ignore`
//! files. Hidden files are skipped unless the caller asks for them (`Hidden::Included`),
//! and then `.git` is still left out. Directories are never visited (the walk descends
//! into them); everything else is, symlinks included and not followed.
//!
//! A glob filters what is visited. One without a `/` is matched against the file
//! name; one with a `/` against the path below the walked directory.

use std::path::{Component, Path, PathBuf};

use globset::{GlobBuilder, GlobMatcher};
pub(crate) use ignore::DirEntry as Item; // rules: allow(entry): the `ignore` crate's name
use ignore::{WalkBuilder, WalkState};

use crate::{Result, ToolError};

/// Whether the walk visits hidden files and the contents of hidden directories.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Hidden {
    Skipped,
    /// Listed and entered, except `.git` below the walked directory.
    Included,
}

/// A glob, and whether it has a `/` (so it is matched against the path below the walked
/// directory instead of the file name).
pub(crate) struct Glob {
    matcher: GlobMatcher,
    slashed: bool,
}

impl Glob {
    /// # Errors
    /// The glob does not parse, or is an absolute path (it would match nothing).
    pub(crate) fn new(glob: &str) -> Result<Self> {
        if glob.starts_with('/') {
            return Err(ToolError(format!(
                "the glob `{glob}` is an absolute path, but a glob is relative to `path`: \
                 pass the directory as `path` and the rest as the glob"
            )));
        }
        let slashed = glob.contains('/');
        let matcher = GlobBuilder::new(glob.trim_start_matches("./"))
            .literal_separator(slashed)
            .build()
            .map_err(|e| ToolError(format!("invalid glob: {e}")))?
            .compile_matcher();
        Ok(Self { matcher, slashed })
    }

    fn allows(&self, root: &Path, path: &Path) -> bool {
        if self.slashed {
            self.matcher
                .is_match(path.strip_prefix(root).unwrap_or(path))
        } else {
            path.file_name()
                .is_some_and(|name| self.matcher.is_match(Path::new(name)))
        }
    }
}

/// Walk the directory `root`, calling the visitor `make` returns (one per walker thread) for
/// every item that is not a directory and that `glob` allows, hidden ones as `hidden` says. `stop` is asked before each
/// item; the walk ends once it says so. A path the walk cannot read goes to `on_error`.
pub(crate) fn walk<'s, V>(
    root: &'s Path,
    glob: Option<&'s Glob>,
    hidden: Hidden,
    stop: &'s (dyn Fn() -> bool + Sync),
    on_error: &'s (dyn Fn(String) + Sync),
    make: impl Fn() -> V + Sync + 's,
) where
    V: FnMut(&Item) + Send + 's,
{
    let mut builder = WalkBuilder::new(root);
    // .gitignore applies in any tree, not only inside a git repository.
    builder.require_git(false);
    if hidden == Hidden::Included {
        builder.hidden(false);
        builder.filter_entry(|e| e.depth() == 0 || e.file_name() != ".git"); // rules: allow(entry): the `ignore` crate's name
    }
    builder.build_parallel().run(|| {
        let mut visit = make();
        Box::new(move |found| {
            if stop() {
                return WalkState::Quit;
            }
            match found {
                Ok(item) if !item.file_type().is_some_and(|t| t.is_dir()) => {
                    if glob.is_none_or(|g| g.allows(root, item.path())) {
                        visit(&item);
                    }
                }
                Ok(_) => {}
                Err(e) => on_error(e.to_string()),
            }
            WalkState::Continue
        })
    });
}

/// `path` without `.` components, so that it can be shown relative to the working
/// directory.
pub(crate) fn clean(path: &Path) -> PathBuf {
    path.components()
        .filter(|c| *c != Component::CurDir)
        .collect()
}
