use std::{
    fmt,
    hash::{Hash, Hasher},
    marker::PhantomData,
};

use thiserror::Error;

/// A positive numeric receipt key, scoped to its entity type.
pub struct Id<T> {
    value: u64,
    entity: PhantomData<fn() -> T>,
}

/// A durable receipt key cannot be zero.
#[derive(Debug, Error)]
#[error("invalid receipt id: expected a positive integer")]
pub(crate) struct ZeroError;

impl<T> Id<T> {
    /// Construct a key from a persisted receipt number.
    pub(crate) fn new(value: u64) -> Result<Self, ZeroError> {
        if value == 0 {
            return Err(ZeroError);
        }
        Ok(Self {
            value,
            entity: PhantomData,
        })
    }

    /// Return the number used by the local storage and notification protocols.
    pub fn get(self) -> u64 {
        self.value
    }
}

impl<T> fmt::Debug for Id<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.value.fmt(f)
    }
}
impl<T> Clone for Id<T> {
    fn clone(&self) -> Self {
        *self
    }
}
impl<T> Copy for Id<T> {}
impl<T> PartialEq for Id<T> {
    fn eq(&self, other: &Self) -> bool {
        self.value == other.value
    }
}
impl<T> Eq for Id<T> {}
impl<T> Hash for Id<T> {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.value.hash(state);
    }
}
