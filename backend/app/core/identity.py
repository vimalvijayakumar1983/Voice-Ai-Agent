"""Canonical identity helpers shared by schemas, routes, and migrations."""


def normalize_email(value: object) -> str:
    """Return the application's canonical representation of an email address.

    Email lookup and uniqueness are intentionally case-insensitive.  Keeping the
    normalization in one helper prevents registration, login, and invitations
    from drifting into subtly different account namespaces.
    """

    return str(value).strip().lower()
