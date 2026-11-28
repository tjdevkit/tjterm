use crate::hosts::Transport;
use serde::Serialize;
use std::{
    convert::Infallible,
    io::{self, Write},
    path::Path,
};
use thiserror::Error;
use tj_listener::domain::{
    delivery::{Failure, Notice, Receipt, Submission},
    owner::Host,
};

pub(crate) struct Monitor;

#[derive(Serialize)]
struct Event<'a> {
    id: u64,
    item: &'a str,
    pr: &'a str,
    revision: &'a str,
    reason: &'a str,
    profile: &'a Path,
    thread: String,
}

#[derive(Debug, Error)]
pub(crate) enum SendError {
    #[error("could not encode the Claude monitor event")]
    Encode(#[source] serde_json::Error),
    #[error("could not finish writing the Claude monitor event")]
    Write(#[source] io::Error),
}

impl Transport for Monitor {
    const HOST: Host = Host::Claude;
    type PreflightError = Infallible;
    type SendError = SendError;

    fn prepare(&self, _: &Notice) -> Result<(), Infallible> {
        Ok(())
    }

    fn send(&self, notice: &Notice) -> Submission<SendError> {
        let event = Event {
            id: notice.id().get(),
            item: notice.item().as_str(),
            pr: notice.pr().as_str(),
            revision: notice.revision().as_str(),
            reason: notice.reason(),
            profile: notice.owner().profile(),
            thread: notice.owner().thread().to_string(),
        };
        let mut line =
            serde_json::to_vec(&event).map_err(|e| Failure::Unsubmitted(SendError::Encode(e)))?;
        line.push(b'\n');
        let mut stdout = io::stdout().lock();
        stdout
            .write_all(&line)
            .and_then(|()| stdout.flush())
            .map_err(|e| Failure::Uncertain(SendError::Write(e)))?;
        Ok(Receipt::Emitted)
    }
}
