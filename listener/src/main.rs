//! Native listener for linked pull request workflows.
mod cli;
mod hosts;

fn main() -> anyhow::Result<()> {
    cli::run()
}
