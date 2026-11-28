//! Process double for the native Codex CLI and its Python preflight interpreter.

use std::{env, fs::OpenOptions, io::Write, process};

fn main() {
    let mut calls = OpenOptions::new()
        .create(true)
        .append(true)
        .open(env::var_os("TJ_FIXTURE_CALLS").expect("fixture call log"))
        .expect("open fixture call log");
    let args: Vec<_> = env::args().skip(1).collect();
    writeln!(calls, "{}", args.join(" ")).expect("record invocation");
    if args.starts_with(&["-I".into(), "-m".into(), "tjentic.watch.preflight".into()]) {
        eprintln!("fixture rejected missing project standards");
        process::exit(1);
    }
}
