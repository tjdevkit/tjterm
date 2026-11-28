//! Native executables for the watch integration tests, isolated by each process's PATH.

use std::{
    env, fs,
    io::Write,
    path::{Path, PathBuf},
    process, thread,
    time::{Duration, Instant},
};

fn signal_and_wait(directory: &Path, name: &str) {
    fs::write(directory.join(name), "").expect("signal the test process");
    let deadline = Instant::now() + Duration::from_secs(15);
    while !directory.join("release").exists() {
        assert!(Instant::now() < deadline, "the fixture was not released");
        thread::sleep(Duration::from_millis(5));
    }
}

fn main() {
    let directory =
        PathBuf::from(env::var_os("TJ_FIXTURE_DIR").expect("temporary fixture directory"));
    let args: Vec<_> = env::args().skip(1).collect();
    let mut calls = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(directory.join("calls"))
        .expect("open fixture call log");
    writeln!(calls, "{}", args.join(" ")).expect("record invocation");
    let mode = env::var("TJ_FIXTURE_MODE").expect("fixture response");
    match args.first().map(String::as_str) {
        Some("api") => print!(
            "{}",
            fs::read_to_string(directory.join("forge.json")).unwrap()
        ),
        Some("-I")
            if mode == "refuse"
                || (mode == "refuse-one"
                    && args
                        .iter()
                        .any(|arg| arg == "39b094709f9b4fc697299dc049d8a390")) =>
        {
            eprintln!("fixture rejected missing project standards");
            process::exit(1);
        }
        Some("-I") if mode == "remove-codex" => {
            fs::remove_file(directory.join(format!("codex{}", env::consts::EXE_SUFFIX))).unwrap();
        }
        Some("-I") if mode == "pause-preflight" => signal_and_wait(&directory, "preflight"),
        Some("queue") if mode == "pause-queue" => signal_and_wait(&directory, "submitted"),
        Some("queue") if mode == "unknown" => {
            eprintln!("fixture lost the native receipt after submission");
            process::exit(1);
        }
        _ => {}
    }
}
