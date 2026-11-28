use crate::domain::forge::{Oid, OidError};
use file_id::FileId;
use std::{
    io,
    path::{Path, PathBuf},
    process::Command,
    string::FromUtf8Error,
};
use thiserror::Error;

/// A checkout bound to the filesystem identity of its per-worktree Git directory.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Worktree {
    root: PathBuf,
    git_dir: PathBuf,
    device: u64,
    inode: u128,
}

/// A stored Git binding has no portable, absolute path representation.
#[derive(Debug, Error)]
#[error("worktree paths must be absolute UTF-8 paths")]
pub struct PathError;

/// A local Git query could not return its requested value.
#[derive(Debug, Error)]
pub enum GitError {
    /// Git could not be invoked.
    #[error("could not execute git")]
    Execute(#[source] io::Error),
    /// Git rejected the query.
    #[error("git rev-parse {option} failed: {message}")]
    Rejected {
        /// The requested revision or path.
        option: &'static str,
        /// Git's diagnostic.
        message: String,
    },
    /// The database and native clients require a Unicode path.
    #[error("git returned a non-UTF-8 value")]
    Encoding(#[source] FromUtf8Error),
}

/// A repository or its per-worktree identity could not be inspected.
#[derive(Debug, Error)]
pub enum DiscoverError {
    /// The checkout root could not be located.
    #[error("could not locate the Git worktree root")]
    Root(#[source] GitError),
    /// The per-worktree metadata directory could not be located.
    #[error("could not locate the Git directory for this worktree")]
    Directory(#[source] GitError),
    /// The metadata directory could not be identified on disk.
    #[error("could not identify the registered Git directory")]
    Identity(#[source] io::Error),
    /// Git returned paths which cannot be bound to a workflow.
    #[error(transparent)]
    Path(#[from] PathError),
}

/// A checked-out revision could not be compared with a pull request.
#[derive(Debug, Error)]
pub enum HeadError {
    /// The checkout has no readable commit at HEAD.
    #[error("could not read the checked-out Git revision")]
    Git(#[source] GitError),
    /// Git returned an invalid object identifier.
    #[error(transparent)]
    Oid(#[from] OidError),
}

fn rev_parse(directory: &Path, option: &'static str) -> Result<String, GitError> {
    let output = Command::new("git")
        .arg("-C")
        .arg(directory)
        .args(["rev-parse", option])
        .output()
        .map_err(GitError::Execute)?;
    if !output.status.success() {
        return Err(GitError::Rejected {
            option,
            message: String::from_utf8_lossy(&output.stderr)
                .trim_end()
                .to_owned(),
        });
    }
    let mut value = String::from_utf8(output.stdout).map_err(GitError::Encoding)?;
    if value.ends_with('\n') {
        value.pop();
    }
    Ok(value)
}

impl Worktree {
    /// Resolve the same binding from the root or any subdirectory of a linked checkout.
    pub fn discover(directory: &Path) -> Result<Self, DiscoverError> {
        let root =
            PathBuf::from(rev_parse(directory, "--show-toplevel").map_err(DiscoverError::Root)?);
        let git_dir = PathBuf::from(
            rev_parse(directory, "--absolute-git-dir").map_err(DiscoverError::Directory)?,
        );
        let (device, inode) =
            match file_id::get_file_id(&git_dir).map_err(DiscoverError::Identity)? {
                FileId::Inode {
                    device_id,
                    inode_number,
                } => (device_id, u128::from(inode_number)),
                FileId::LowRes {
                    volume_serial_number,
                    file_index,
                } => (u64::from(volume_serial_number), u128::from(file_index)),
                FileId::HighRes {
                    volume_serial_number,
                    file_id,
                } => (volume_serial_number, file_id),
            };
        // Git prints forward slashes on Windows. Bind the same native spelling as Python.
        if !root.is_absolute() || !git_dir.is_absolute() {
            return Err(DiscoverError::Path(PathError));
        }
        Ok(Self {
            root: root.components().collect(),
            git_dir: git_dir.components().collect(),
            device,
            inode,
        })
    }

    /// Return the directory where the item is registered.
    pub fn root(&self) -> &Path {
        &self.root
    }
    /// Return this worktree's own Git metadata directory.
    pub fn git_dir(&self) -> &Path {
        &self.git_dir
    }
    /// Return the metadata directory's device number or volume serial number.
    pub fn device(&self) -> u64 {
        self.device
    }
    /// Return the metadata directory's inode or Windows file identifier.
    pub fn inode(&self) -> u128 {
        self.inode
    }
    /// Read the commit currently checked out in this worktree.
    pub fn head(&self) -> Result<Oid, HeadError> {
        Ok(Oid::parse(
            &rev_parse(&self.root, "HEAD^{commit}").map_err(HeadError::Git)?,
        )?)
    }
}
