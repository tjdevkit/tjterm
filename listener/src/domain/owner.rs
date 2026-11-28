use std::{
    path::{Path, PathBuf},
    str::FromStr,
};

use thiserror::Error;
use uuid::Uuid;

/// A native CLI that can own an item.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Host {
    /// Claude Code.
    Claude,
    /// The Codex CLI.
    Codex,
}

/// The host is outside the listener's supported native providers.
#[derive(Debug, Error)]
#[error("unsupported native host {0:?}")]
pub struct HostError(String);

impl Host {
    /// Resolve the spelling used on the command line and in stored bindings.
    pub fn parse(host: &str) -> Result<Self, HostError> {
        match host {
            "codex" => Ok(Self::Codex),
            "claude" => Ok(Self::Claude),
            _ => Err(HostError(host.to_owned())),
        }
    }
    /// Borrow the stable provider key.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }
}

impl FromStr for Host {
    type Err = HostError;
    fn from_str(host: &str) -> Result<Self, Self::Err> {
        Self::parse(host)
    }
}

/// A native session is identified by its provider, profile, and immutable thread UUID.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Owner {
    host: Host,
    profile: String,
    thread: Uuid,
}

/// Profile validation for a binding shared by the native CLI and the workflow store.
#[derive(Debug, Error)]
pub enum ProfileError {
    /// A relative profile would resolve differently after the service restarts.
    #[error("native owner profile must be an absolute path")]
    Relative,
    /// The registered profile cannot be represented in the shared workflow database.
    #[error("native owner profile must be a UTF-8 path")]
    Encoding,
}

impl Owner {
    /// Bind a native thread to its explicit provider profile.
    pub fn new(host: Host, profile: PathBuf, thread: Uuid) -> Result<Self, ProfileError> {
        if !profile.is_absolute() {
            return Err(ProfileError::Relative);
        }
        let profile = profile
            .into_os_string()
            .into_string()
            .map_err(|_| ProfileError::Encoding)?;
        Ok(Self {
            host,
            profile,
            thread,
        })
    }
    /// Select the adapter that delivers to this session.
    pub fn host(&self) -> Host {
        self.host
    }
    /// Identify the native account directory for operations on this thread.
    pub fn profile(&self) -> &Path {
        Path::new(&self.profile)
    }
    /// Borrow the exact UTF-8 path used by the storage protocol.
    pub(crate) fn profile_str(&self) -> &str {
        &self.profile
    }
    /// Identify the exact native conversation.
    pub fn thread(&self) -> Uuid {
        self.thread
    }
}
