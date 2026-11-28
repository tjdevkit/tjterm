use std::error::Error;
use tj_listener::domain::{
    delivery::{Notice, Submission},
    owner::Host,
};

pub(crate) mod claude {
    pub(crate) mod watch;
}
pub(crate) mod codex {
    pub(crate) mod watch;
}

pub(crate) trait Transport {
    const HOST: Host;
    type PreflightError: Error + Send + Sync + 'static;
    type SendError: Error + Send + Sync + 'static;

    fn prepare(&self, notice: &Notice) -> Result<(), Self::PreflightError>;
    fn send(&self, notice: &Notice) -> Submission<Self::SendError>;
}
