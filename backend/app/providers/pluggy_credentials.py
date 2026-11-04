"""Per-workspace Pluggy credentials.

A single Securo instance may serve several workspaces, and each workspace can
use its own Pluggy client (client_id + client_secret). The mapping lives in a
JSON file pointed to by PLUGGY_WORKSPACE_CREDENTIALS_FILE; workspaces without
an entry fall back to the instance-wide credentials.

Errors raised here never carry the client secret: they go to logs and startup
output, and the secret must not surface there.
"""

import functools
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class PluggyCredentials:
    """Pluggy API client credentials for one credential set."""

    client_id: str
    # Kept out of repr() so the secret cannot leak into logs or tracebacks.
    client_secret: str = field(repr=False)


class PluggyCredentialsError(ValueError):
    """Raised when workspace Pluggy credentials are missing or malformed."""


def parse_workspace_credentials(raw: str, source: str) -> dict[uuid.UUID, PluggyCredentials]:
    """Parse a JSON object mapping workspace UUIDs to Pluggy credentials.

    Raises PluggyCredentialsError naming `source`, the offending key and the
    field; the client secret is never part of the message.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PluggyCredentialsError(f"{source}: invalid JSON: {e.msg}") from None

    if not isinstance(data, dict):
        raise PluggyCredentialsError(f"{source}: expected a JSON object at the top level")

    credentials: dict[uuid.UUID, PluggyCredentials] = {}
    for key, value in data.items():
        try:
            workspace_id = uuid.UUID(key)
        except (AttributeError, TypeError, ValueError):
            raise PluggyCredentialsError(
                f"{source}: workspace key {key!r} is not a valid UUID"
            ) from None

        if not isinstance(value, dict):
            raise PluggyCredentialsError(f"{source}: workspace {key} must map to a JSON object")

        client_id = value.get("client_id")
        client_secret = value.get("client_secret")
        if not isinstance(client_id, str) or not client_id.strip():
            raise PluggyCredentialsError(
                f"{source}: workspace {key} has an empty or missing 'client_id'"
            )
        if not isinstance(client_secret, str) or not client_secret.strip():
            raise PluggyCredentialsError(
                f"{source}: workspace {key} has an empty or missing 'client_secret'"
            )

        credentials[workspace_id] = PluggyCredentials(
            client_id=client_id.strip(), client_secret=client_secret.strip()
        )

    return credentials


def load_workspace_credentials(path: str) -> dict[uuid.UUID, PluggyCredentials]:
    """Load the workspace credential map from a JSON file.

    A blank path means "no per-workspace credentials" and yields an empty map.
    Raises PluggyCredentialsError when the file cannot be read or is malformed.
    """
    if not path.strip():
        return {}

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise PluggyCredentialsError(
            f"{path}: cannot read credentials file ({e.strerror})"
        ) from None

    return parse_workspace_credentials(raw, path)


@functools.lru_cache(maxsize=1)
def get_workspace_credentials() -> dict[uuid.UUID, PluggyCredentials]:
    """Return the workspace credential map, cached for the process lifetime."""
    return load_workspace_credentials(get_settings().pluggy_workspace_credentials_file)


def instance_credentials() -> PluggyCredentials | None:
    """Return the instance-wide Pluggy credentials, or None when unset."""
    settings = get_settings()
    if settings.pluggy_client_id and settings.pluggy_client_secret.get_secret_value():
        return PluggyCredentials(
            client_id=settings.pluggy_client_id,
            client_secret=settings.pluggy_client_secret.get_secret_value(),
        )
    return None


def resolve_pluggy_credentials(workspace_id: uuid.UUID | None) -> PluggyCredentials:
    """Return the credentials for a workspace, falling back to the instance ones.

    A workspace present in the file never falls back when its entry has no
    usable credentials. Raises PluggyCredentialsError when neither is configured.
    """
    if workspace_id is not None:
        credentials = get_workspace_credentials().get(workspace_id)
        if credentials is not None:
            return credentials

    credentials = instance_credentials()
    if credentials is None:
        raise PluggyCredentialsError("Pluggy is not configured for this workspace")
    return credentials
