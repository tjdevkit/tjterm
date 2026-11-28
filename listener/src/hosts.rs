use anyhow::Result;
use clap::ValueEnum;
use std::path::Path;
use tj_listener::store::{Notice, Store};

mod claude {
    pub(super) mod watch;
}
mod codex {
    pub(super) mod watch;
}

#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum Host {
    Claude,
    Codex,
}
impl Host {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }
    pub(crate) fn deliver(
        self,
        store: &mut Store,
        profile: Option<&str>,
        thread: Option<&str>,
        state: &Path,
        python: &Path,
    ) -> Result<bool> {
        let Some(notice) = store.claim_notice(self.name(), profile, thread)? else {
            return Ok(false);
        };
        let send: fn(&Notice) -> Result<&'static str> = match self {
            Self::Claude => claude::watch::send,
            Self::Codex => {
                if let Err(error) = codex::watch::prepare(&notice, state, python) {
                    store.retry_unsubmitted(notice.id)?;
                    return Err(error);
                }
                codex::watch::send
            }
        };
        store.dispatch(&notice, send)?;
        Ok(true)
    }
}
