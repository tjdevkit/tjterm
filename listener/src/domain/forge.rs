use thiserror::Error;

const GITHUB: &str = "https://github.com/";

/// A GitHub URL must identify a single issue or pull request.
#[derive(Debug, Error)]
pub enum ReferenceError {
    /// The URL does not have the scheme, host and path of the expected resource.
    #[error("expected https://github.com/OWNER/REPO/{0}/NUMBER")]
    Format(&'static str),
    /// At least one repository component contains an unsupported character.
    #[error("invalid repository name")]
    Repository,
    /// GitHub numbers are positive decimal integers.
    #[error("invalid GitHub issue or pull request number")]
    Number,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Reference {
    url: String,
    owner_end: usize,
    repo_end: usize,
    number: u32,
}

impl Reference {
    fn parse(url: &str, kind: &'static str) -> Result<Self, ReferenceError> {
        let mut parts = url
            .strip_prefix(GITHUB)
            .ok_or(ReferenceError::Format(kind))?
            .split('/');
        let (Some(owner), Some(repo), Some(resource), Some(number), None) = (
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
        ) else {
            return Err(ReferenceError::Format(kind));
        };
        if resource != kind {
            return Err(ReferenceError::Format(kind));
        }
        for part in [owner, repo] {
            if matches!(part, "" | "." | "..")
                || !part
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
            {
                return Err(ReferenceError::Repository);
            }
        }
        // Preview issue URLs retained the spelling accepted by u32::from_str.
        // Pull request links come from GitHub and retain their unsigned URL syntax.
        if kind == "pull" && !number.bytes().all(|c| c.is_ascii_digit()) {
            return Err(ReferenceError::Number);
        }
        let number = number
            .parse()
            .ok()
            .filter(|&number| number > 0)
            .ok_or(ReferenceError::Number)?;
        let owner_end = GITHUB.len() + owner.len();
        Ok(Self {
            url: url.to_owned(),
            owner_end,
            repo_end: owner_end + 1 + repo.len(),
            number,
        })
    }
}

/// The issue whose closing pull requests supply observations for the wait.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IssueRef(Reference);

impl IssueRef {
    /// Validate the HTTPS URL of a GitHub issue.
    pub fn parse(url: &str) -> Result<Self, ReferenceError> {
        Reference::parse(url, "issues").map(Self)
    }
    /// Borrow the original URL used to persist this link.
    pub fn as_str(&self) -> &str {
        &self.0.url
    }
    pub(crate) fn owner(&self) -> &str {
        &self.0.url[GITHUB.len()..self.0.owner_end]
    }
    pub(crate) fn repo(&self) -> &str {
        &self.0.url[self.0.owner_end + 1..self.0.repo_end]
    }
    pub(crate) fn number(&self) -> u32 {
        self.0.number
    }
}

/// A link to one specific GitHub pull request.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PullRef(Reference);

impl PullRef {
    /// Validate the HTTPS URL of a GitHub pull request.
    pub fn parse(url: &str) -> Result<Self, ReferenceError> {
        Reference::parse(url, "pull").map(Self)
    }
    /// Borrow the original URL used to persist this link.
    pub fn as_str(&self) -> &str {
        &self.0.url
    }
}

/// A complete SHA-1 or SHA-256 Git object identifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Oid(String);

/// The candidate revision is not a full hexadecimal Git object name.
#[derive(Debug, Error)]
#[error("invalid Git object id: expected 40 or 64 hexadecimal digits")]
pub struct OidError;

impl Oid {
    /// Accept full object names and normalize hexadecimal case.
    pub fn parse(value: &str) -> Result<Self, OidError> {
        if matches!(value.len(), 40 | 64) && value.bytes().all(|c| c.is_ascii_hexdigit()) {
            Ok(Self(value.to_ascii_lowercase()))
        } else {
            Err(OidError)
        }
    }
    /// Borrow the canonical hexadecimal encoding.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// The state of a linked pull request at the time GitHub answered.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum State {
    /// The pull request is still open.
    Open,
    /// The pull request was closed without a merge.
    Closed,
    /// GitHub confirmed the merge and identified the resulting commit.
    Merged(Oid),
}

/// One observed pull request and its most recent head.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Pull {
    reference: PullRef,
    head: Oid,
    state: State,
}

impl Pull {
    /// Capture an observation of the given candidate.
    pub fn new(reference: PullRef, head: Oid, state: State) -> Self {
        Self {
            reference,
            head,
            state,
        }
    }
    /// Identify the observed pull request.
    pub fn reference(&self) -> &PullRef {
        &self.reference
    }
    /// Identify the candidate revision seen by GitHub.
    pub fn head(&self) -> &Oid {
        &self.head
    }
    /// Borrow the lifecycle information included in this observation.
    pub fn state(&self) -> &State {
        &self.state
    }
}

/// Whether the issue still identifies a single candidate without ambiguity.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Links {
    /// Exactly one pull request closes the issue.
    Single(Pull),
    /// The issue has zero or several closing pull requests.
    NotSingle,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn references_identify_one_expected_resource() {
        let issue = IssueRef::parse("https://github.com/tools/engine/issues/13").unwrap();
        assert_eq!(
            (issue.owner(), issue.repo(), issue.number()),
            ("tools", "engine", 13)
        );
        let preview_url = "https://github.com/tools/engine/issues/+0010";
        let preview = IssueRef::parse(preview_url).unwrap();
        assert_eq!(preview.as_str(), preview_url);
        assert_eq!(preview.number(), 10);
        for url in [
            "https://github.com/tools/engine/issues/13",
            "https://github.com/tools/engine/pull/0",
            "https://github.com/tools/engine/pull/1?query",
            "https://github.com/tools/../pull/1",
            "https://github.com/tools/engine/pull/+1",
        ] {
            assert!(PullRef::parse(url).is_err());
        }
    }

    #[test]
    fn accepts_full_git_names_and_canonicalizes_case() {
        for len in [40, 64] {
            assert_eq!(
                Oid::parse(&"A".repeat(len)).unwrap().as_str(),
                "a".repeat(len)
            );
        }
        assert!(Oid::parse(&"0".repeat(39)).is_err());
        assert!(Oid::parse(&"g".repeat(40)).is_err());
    }
}
