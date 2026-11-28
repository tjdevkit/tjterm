mod hosts;

use anyhow::{Context, Result, ensure};
use clap::{Args, Parser, Subcommand};
use hosts::Host;
use std::{
    fs::OpenOptions,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Duration,
};
use tj_listener::github::{self, Issue};
use tj_listener::store::{Binding, Store};
use uuid::Uuid;

#[derive(Parser)]
struct Cli {
    #[arg(long)]
    state: PathBuf,
    /// Interpreter from the TJ installation that started this listener.
    #[arg(long)]
    python: Option<PathBuf>,
    #[command(subcommand)]
    command: Action,
}

#[derive(Args)]
struct Owner {
    #[arg(long)]
    host: Host,
    #[arg(long)]
    profile: PathBuf,
    #[arg(long)]
    thread: Uuid,
}

#[derive(Subcommand)]
enum Action {
    Init,
    Ensure,
    Serve,
    Reconcile,
    Register {
        #[command(flatten)]
        owner: Owner,
        #[arg(long)]
        item: String,
        #[arg(long)]
        issue: String,
        #[arg(long)]
        directory: PathBuf,
        #[arg(long)]
        device: String,
        #[arg(long)]
        inode: String,
    },
    Watch {
        #[command(flatten)]
        owner: Owner,
        #[arg(long)]
        once: bool,
    },
}

fn reconcile(db: &mut Store, state: &Path, python: &Path) -> Result<()> {
    for wait in db.waits()? {
        let result = Issue::parse(&wait.issue)
            .and_then(|issue| issue.read())
            .and_then(|links| db.observe(&wait, &links));
        if let Err(error) = result {
            db.error(&wait, &error.to_string())?;
        }
    }
    while Host::Codex.deliver(db, None, None, state, python)? {}
    Ok(())
}

fn lock_file(state: &Path) -> Result<std::fs::File> {
    Ok(OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(state.join("pr-listener.lock"))?)
}

fn start(state: &Path, python: &Path) -> Result<()> {
    let lock = lock_file(state)?;
    if let Err(e) = lock.try_lock() {
        return match e {
            std::fs::TryLockError::WouldBlock => Ok(()),
            std::fs::TryLockError::Error(e) => Err(e.into()),
        };
    }
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.join("pr-listener.log"))?;
    let mut child = Command::new(std::env::current_exe()?);
    child
        .arg("--state")
        .arg(state)
        .arg("--python")
        .arg(python)
        .arg("serve")
        .current_dir(state)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        child.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        child.creation_flags(0x0800_0200);
    }
    // The service elects a single owner under the same lock after this short launch guard.
    drop(lock);
    child.spawn()?;
    Ok(())
}

fn serve(state: &Path, db: &mut Store, python: &Path) -> Result<()> {
    let lock = lock_file(state)?;
    match lock.try_lock() {
        Ok(()) => (),
        Err(std::fs::TryLockError::WouldBlock) => return Ok(()),
        Err(std::fs::TryLockError::Error(e)) => return Err(e.into()),
    }
    let mut interval = Duration::from_secs(15);
    loop {
        if let Err(e) = reconcile(db, state, python) {
            eprintln!("{e:#}");
        }
        if db.waits()?.is_empty() {
            // Registration commits before starting a service. Recheck after relinquishing ownership.
            drop(lock);
            if !db.waits()?.is_empty() {
                start(state, python)?;
            }
            return Ok(());
        }
        thread::sleep(interval);
        interval = (interval * 2).min(Duration::from_mins(2));
    }
}

fn subscription_file(state: &Path, owner: &Owner) -> Result<std::fs::File> {
    use std::hash::{DefaultHasher, Hash, Hasher};
    let mut hash = DefaultHasher::new();
    owner.profile.hash(&mut hash);
    let name = format!(
        "watch-{}-{}-{:x}.lock",
        owner.host.name(),
        owner.thread,
        hash.finish()
    );
    Ok(OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(state.join(name))?)
}

fn claude_subscribed(state: &Path, owner: &Owner) -> Result<bool> {
    let file = subscription_file(state, owner)?;
    match file.try_lock() {
        Ok(()) => Ok(false),
        Err(std::fs::TryLockError::WouldBlock) => Ok(true),
        Err(std::fs::TryLockError::Error(e)) => Err(e.into()),
    }
}

fn main() -> Result<()> {
    let args = Cli::parse();
    let python = match args.python {
        Some(path) => path,
        None => std::env::current_exe()?
            .with_file_name(format!("python{}", std::env::consts::EXE_SUFFIX)),
    };
    ensure!(
        python.is_absolute(),
        "the TJ interpreter path must be absolute"
    );
    let mut db = Store::open(&args.state)?;
    match args.command {
        Action::Init => Ok(()),
        Action::Ensure => {
            if !db.waits()?.is_empty() {
                start(&args.state, &python)?;
            }
            Ok(())
        }
        Action::Serve => serve(&args.state, &mut db, &python),
        Action::Reconcile => reconcile(&mut db, &args.state, &python),
        Action::Register {
            owner,
            item,
            issue,
            directory,
            device,
            inode,
        } => {
            ensure!(
                !item.trim().is_empty() && owner.profile.is_absolute(),
                "invalid item or owner profile"
            );
            let links = Issue::parse(&issue)?.read()?;
            let pr = links
                .single()
                .context("the issue must have exactly one linked pull request")?;
            let revision = github::git(&directory, &["rev-parse", "--verify", "HEAD"])?;
            ensure!(
                pr.head_ref_oid == revision,
                "the linked PR head differs from the checked-out revision"
            );
            let root = github::git(&directory, &["rev-parse", "--show-toplevel"])?;
            ensure!(
                Path::new(&root).canonicalize()? == directory.canonicalize()?,
                "registration requires the bound worktree root"
            );
            let root = directory.to_str().context("invalid worktree path")?;
            let git_dir = github::git(&directory, &["rev-parse", "--absolute-git-dir"])?;
            let wait = db.bind(&Binding {
                item: &item,
                issue: &issue,
                pr: &pr.url,
                revision: &revision,
                host: owner.host.name(),
                profile: owner.profile.to_str().context("invalid profile path")?,
                thread: &owner.thread.to_string(),
                root,
                git_dir: &git_dir,
                device: &device,
                inode: &inode,
            })?;
            // The first observation may already be merged. Serializing it with normal observations is safe.
            db.observe(&wait, &links)?;
            if db.has_closeout(&wait)? {
                println!("Local closeout recorded for {item} at {revision}.");
                return Ok(());
            }
            start(&args.state, &python)?;
            if matches!(owner.host, Host::Claude) && !claude_subscribed(&args.state, &owner)? {
                println!(
                    "For this session, start native Bash with run_in_background=true and command `tj watch --once` to receive an event if the PR needs attention."
                );
            }
            println!("Registered {item} at {revision} for {}.", pr.url);
            Ok(())
        }
        Action::Watch { owner, once } => {
            let subscription = subscription_file(&args.state, &owner)?;
            subscription.lock_shared()?;
            let profile = owner.profile.to_str().context("invalid profile path")?;
            let thread = owner.thread.to_string();
            loop {
                if owner.host.deliver(
                    &mut db,
                    Some(profile),
                    Some(&thread),
                    &args.state,
                    &python,
                )? && once
                {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        }
    }
}
