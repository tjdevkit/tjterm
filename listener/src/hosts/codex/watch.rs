use crate::hosts::Transport;
use std::{
    io,
    path::Path,
    process::{Command, Stdio},
};
use thiserror::Error;
use tj_listener::domain::{
    delivery::{Failure, Notice, Receipt, Submission},
    owner::Host,
};

pub(crate) struct Queue<'a> {
    pub(crate) state: &'a Path,
    pub(crate) python: &'a Path,
}

#[derive(Debug, Error)]
pub(crate) enum PrepareError {
    #[error("could not start the Codex daemon command")]
    StartDaemon(#[source] io::Error),
    #[error("could not run the Codex wake preflight")]
    StartCheck(#[source] io::Error),
    #[error("Codex wake preflight failed: {0}")]
    Rejected(String),
}

#[derive(Debug, Error)]
pub(crate) enum SendError {
    #[error("could not start codex queue")]
    Start(#[source] io::Error),
    #[error("could not read the Codex queue acknowledgement")]
    Acknowledgement(#[source] io::Error),
    #[error("Codex queue failed: {0}")]
    Rejected(String),
}

impl Transport for Queue<'_> {
    const HOST: Host = Host::Codex;
    type PreflightError = PrepareError;
    type SendError = SendError;

    fn prepare(&self, notice: &Notice) -> Result<(), PrepareError> {
        let owner = notice.owner();
        let server = Command::new("codex")
            .args(["app-server", "daemon", "start"])
            .env("CODEX_HOME", owner.profile())
            .output()
            .map_err(PrepareError::StartDaemon)?;
        if !server.status.success() {
            eprintln!(
                "Codex daemon startup: {}",
                String::from_utf8_lossy(&server.stderr).trim()
            );
        }
        let check = Command::new(self.python)
            .args(["-I", "-m", "tjentic.watch.preflight"])
            .arg(self.state)
            .arg(notice.item_key().as_str())
            .arg(owner.thread().to_string())
            .env("CODEX_HOME", owner.profile())
            .output()
            .map_err(PrepareError::StartCheck)?;
        if !check.status.success() {
            return Err(PrepareError::Rejected(
                String::from_utf8_lossy(&check.stderr).trim().to_owned(),
            ));
        }
        Ok(())
    }

    fn send(&self, notice: &Notice) -> Submission<SendError> {
        let owner = notice.owner();
        let text = format!(
            "TJ workflow observation {} for item {:?}. {} PR {} at {}. Review this under the item's existing authorization.",
            notice.id().get(),
            notice.item().as_str(),
            notice.reason(),
            notice.pr().as_str(),
            notice.revision().as_str()
        );
        let child = Command::new("codex")
            .args([
                "queue",
                "--thread",
                &owner.thread().to_string(),
                "--resume",
                "--message",
                &text,
            ])
            .env("CODEX_HOME", owner.profile())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| Failure::Unsubmitted(SendError::Start(error)))?;
        let output = child
            .wait_with_output()
            .map_err(|error| Failure::Uncertain(SendError::Acknowledgement(error)))?;
        if !output.status.success() {
            return Err(Failure::Uncertain(SendError::Rejected(
                String::from_utf8_lossy(&output.stderr).trim().to_owned(),
            )));
        }
        // A resume warning can follow a successful enqueue. Replaying would duplicate it.
        if !output.stderr.is_empty() {
            eprint!("{}", String::from_utf8_lossy(&output.stderr));
        }
        Ok(Receipt::Queued)
    }
}
