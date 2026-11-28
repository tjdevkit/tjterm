use anyhow::Result;
use std::io::{self, Write};
use tj_listener::store::Notice;

pub(crate) fn send(notice: &Notice) -> Result<&'static str> {
    let mut output = io::stdout().lock();
    serde_json::to_writer(&mut output, notice)?;
    writeln!(output)?;
    output.flush()?;
    Ok("emitted")
}
