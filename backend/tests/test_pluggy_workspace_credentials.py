"""Tests for per-workspace Pluggy credentials and the per-client token cache.

A workspace may carry its own Pluggy client (client_id + client_secret) in a
JSON file pointed to by PLUGGY_WORKSPACE_CREDENTIALS_FILE. Workspaces without
an entry keep using the instance-wide credentials, and the Pluggy API key cache
must be scoped per client_id so two clients never share a token.
"""

import json
import uuid
from contextlib import ExitStack
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.providers import (
    _PROVIDERS,
    _auto_register_providers,
    get_provider,
    register_provider,
)
from app.providers.base import BankProvider
from app.providers.pluggy import PluggyProvider
from app.providers.pluggy_credentials import (
    PluggyCredentials,
    PluggyCredentialsError,
    get_workspace_credentials,
    load_workspace_credentials,
    parse_workspace_credentials,
    resolve_pluggy_credentials,
)

WS_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
WS_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
WS_UNMAPPED = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

# Value used everywhere a test needs a secret, so leaking it is detectable.
SECRET = "s3cr3t-value"


class RecordingProvider(BankProvider):
    """Minimal provider recording the workspace id handed by the registry."""

    received: Optional[uuid.UUID] = None

    @property
    def name(self) -> str:
        return "recording"

    @classmethod
    def for_workspace(cls, workspace_id: uuid.UUID | None) -> "RecordingProvider":
        instance = cls()
        instance.received = workspace_id
        return instance

    async def get_oauth_url(
        self, redirect_uri: str, state: str, flow_params: Optional[dict] = None
    ) -> str:
        return "https://recording.example.com"

    async def handle_oauth_callback(self, code: str):
        return None

    async def get_accounts(self, credentials: dict):
        return []

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since=None,
        payee_source: str = "auto",
    ):
        return []

    async def refresh_credentials(self, credentials: dict) -> dict:
        return credentials


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset the credential file cache and the Pluggy key cache around each test."""
    get_workspace_credentials.cache_clear()
    PluggyProvider._api_keys.clear()
    yield
    get_workspace_credentials.cache_clear()
    PluggyProvider._api_keys.clear()
    _PROVIDERS.pop("recording", None)


@pytest.fixture
def isolated_provider_registry():
    """Empty the provider registry for startup tests and restore it after."""
    saved = dict(_PROVIDERS)
    _PROVIDERS.clear()
    get_workspace_credentials.cache_clear()
    yield
    _PROVIDERS.clear()
    _PROVIDERS.update(saved)
    get_workspace_credentials.cache_clear()


@pytest.fixture
def pluggy_registered():
    """Register the Pluggy provider for registry tests, restoring the previous state."""
    original = _PROVIDERS.get("pluggy")
    register_provider("pluggy", PluggyProvider)
    yield
    if original is None:
        _PROVIDERS.pop("pluggy", None)
    else:
        _PROVIDERS["pluggy"] = original


def _settings(
    pluggy_client_id: str = "",
    pluggy_client_secret: str = "",
    pluggy_workspace_credentials_file: str = "",
) -> Settings:
    """Build Settings isolated from the instance environment."""
    return Settings(
        pluggy_client_id=pluggy_client_id,
        pluggy_client_secret=pluggy_client_secret,
        pluggy_workspace_credentials_file=pluggy_workspace_credentials_file,
    )


def _patched_settings(settings: Settings) -> ExitStack:
    """Patch get_settings where the credential and Pluggy modules read it."""
    stack = ExitStack()
    for target in (
        "app.core.config.get_settings",
        "app.providers.pluggy_credentials.get_settings",
        "app.providers.pluggy.get_settings",
    ):
        stack.enter_context(patch(target, return_value=settings))
    return stack


def _write_credentials_file(tmp_path, payload: dict) -> str:
    path = tmp_path / "workspace_credentials.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _payload() -> dict:
    return {
        str(WS_A): {"client_id": "client-a", "client_secret": SECRET},
        str(WS_B): {"client_id": "client-b", "client_secret": "secret-b"},
    }


def _fake_http(calls: list[dict]) -> MagicMock:
    """Build an httpx.AsyncClient stand-in that records /auth payloads."""

    async def _post(url, **kwargs):
        calls.append({"url": url, "json": kwargs["json"]})
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"apiKey": f"key-for-{kwargs['json']['clientId']}"})
        return response

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# --- Parsing and loading ---------------------------------------------------


def test_valid_file_parses_two_workspaces(tmp_path):
    path = _write_credentials_file(tmp_path, _payload())

    expected = {
        WS_A: PluggyCredentials("client-a", SECRET),
        WS_B: PluggyCredentials("client-b", "secret-b"),
    }
    assert parse_workspace_credentials(json.dumps(_payload()), path) == expected
    assert load_workspace_credentials(path) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{not json", "invalid JSON"),
        ("[]", "JSON object"),
        (
            json.dumps({"not-a-uuid": {"client_id": "i", "client_secret": SECRET}}),
            "not-a-uuid",
        ),
        (json.dumps({str(WS_A): {"client_id": "i"}}), "client_secret"),
        (
            json.dumps({str(WS_A): {"client_id": "   ", "client_secret": SECRET}}),
            "client_id",
        ),
    ],
)
def test_malformed_credentials_raise_without_leaking_the_secret(raw, expected):
    with pytest.raises(PluggyCredentialsError) as excinfo:
        parse_workspace_credentials(raw, "creds.json")

    message = str(excinfo.value)
    assert "creds.json" in message
    assert expected in message
    assert SECRET not in message


def test_two_spellings_of_one_uuid_raise():
    raw = json.dumps(
        {
            str(WS_A): {"client_id": "client-a", "client_secret": SECRET},
            str(WS_A).upper(): {"client_id": "client-b", "client_secret": SECRET},
        }
    )

    with pytest.raises(PluggyCredentialsError) as excinfo:
        parse_workspace_credentials(raw, "creds.json")

    message = str(excinfo.value)
    assert "creds.json" in message
    assert str(WS_A) in message
    assert str(WS_A).upper() in message
    assert SECRET not in message


def test_repeated_workspace_key_raises():
    entry = json.dumps({"client_id": "client-a", "client_secret": SECRET})
    raw = f'{{"{WS_A}": {entry}, "{WS_A}": {entry}}}'

    with pytest.raises(PluggyCredentialsError) as excinfo:
        parse_workspace_credentials(raw, "creds.json")

    message = str(excinfo.value)
    assert "creds.json" in message
    assert str(WS_A) in message
    assert SECRET not in message


def test_repr_hides_the_client_secret():
    assert SECRET not in repr(PluggyCredentials("client-id", SECRET))


def test_empty_path_yields_empty_map():
    assert load_workspace_credentials("") == {}
    assert load_workspace_credentials("   ") == {}


def test_missing_file_error_names_the_path(tmp_path):
    missing = str(tmp_path / "does-not-exist.json")

    with pytest.raises(PluggyCredentialsError) as excinfo:
        load_workspace_credentials(missing)

    assert missing in str(excinfo.value)


# --- Resolution ------------------------------------------------------------


def test_resolve_prefers_workspace_entry_then_instance(tmp_path):
    path = _write_credentials_file(tmp_path, _payload())
    settings = _settings(
        pluggy_client_id="instance-id",
        pluggy_client_secret="instance-secret",
        pluggy_workspace_credentials_file=path,
    )

    with _patched_settings(settings):
        assert resolve_pluggy_credentials(WS_A) == PluggyCredentials("client-a", SECRET)
        assert resolve_pluggy_credentials(WS_B) == PluggyCredentials("client-b", "secret-b")
        assert resolve_pluggy_credentials(WS_UNMAPPED) == PluggyCredentials(
            "instance-id", "instance-secret"
        )
        assert resolve_pluggy_credentials(None) == PluggyCredentials(
            "instance-id", "instance-secret"
        )


def test_resolve_without_mapping_or_instance_raises():
    with _patched_settings(_settings()):
        with pytest.raises(PluggyCredentialsError, match="not configured"):
            resolve_pluggy_credentials(WS_A)


def _pluggy_provider(workspace_id: uuid.UUID | None) -> PluggyProvider:
    """Return the registered Pluggy provider for a workspace, narrowed for typing."""
    provider = get_provider("pluggy", workspace_id)
    assert isinstance(provider, PluggyProvider)
    return provider


# --- Provider registry and token cache -------------------------------------


async def test_get_provider_authenticates_with_the_workspace_client(tmp_path, pluggy_registered):
    path = _write_credentials_file(tmp_path, _payload())
    settings = _settings(
        pluggy_client_id="instance-id",
        pluggy_client_secret="instance-secret",
        pluggy_workspace_credentials_file=path,
    )
    calls: list[dict] = []

    with (
        _patched_settings(settings),
        patch("app.providers.pluggy.httpx.AsyncClient", return_value=_fake_http(calls)),
    ):
        await _pluggy_provider(WS_A)._ensure_api_key()
        assert calls[0]["json"] == {"clientId": "client-a", "clientSecret": SECRET}

        await _pluggy_provider(None)._ensure_api_key()
        assert calls[1]["json"] == {
            "clientId": "instance-id",
            "clientSecret": "instance-secret",
        }


async def test_api_key_cache_is_isolated_per_client(tmp_path, pluggy_registered):
    path = _write_credentials_file(tmp_path, _payload())
    settings = _settings(pluggy_workspace_credentials_file=path)
    calls: list[dict] = []

    with (
        _patched_settings(settings),
        patch("app.providers.pluggy.httpx.AsyncClient", return_value=_fake_http(calls)),
    ):
        key_a = await _pluggy_provider(WS_A)._ensure_api_key()
        key_b = await _pluggy_provider(WS_B)._ensure_api_key()

        assert key_a != key_b
        assert len(calls) == 2
        assert PluggyProvider._api_keys["client-a"][0] == key_a
        assert PluggyProvider._api_keys["client-b"][0] == key_b

        # Same client again: the cached key is reused, no third /auth call.
        assert await _pluggy_provider(WS_A)._ensure_api_key() == key_a
        assert len(calls) == 2


def test_non_pluggy_provider_receives_the_workspace():
    register_provider("recording", RecordingProvider)

    provider = get_provider("recording", WS_A)

    assert isinstance(provider, RecordingProvider)
    assert provider.received == WS_A


# --- Startup registration --------------------------------------------------


def test_auto_register_providers_from_workspace_file_only(tmp_path, isolated_provider_registry):
    path = _write_credentials_file(tmp_path, _payload())
    settings = _settings(pluggy_workspace_credentials_file=path)

    with _patched_settings(settings):
        _auto_register_providers()

    assert _PROVIDERS.get("pluggy") is PluggyProvider


def test_auto_register_providers_rejects_a_malformed_file(tmp_path, isolated_provider_registry):
    path = tmp_path / "workspace_credentials.json"
    path.write_text("{not json", encoding="utf-8")
    settings = _settings(pluggy_workspace_credentials_file=str(path))

    with _patched_settings(settings), pytest.raises(PluggyCredentialsError):
        _auto_register_providers()

    assert "pluggy" not in _PROVIDERS


def test_auto_register_providers_rejects_a_missing_file(tmp_path, isolated_provider_registry):
    settings = _settings(pluggy_workspace_credentials_file=str(tmp_path / "does-not-exist.json"))

    with _patched_settings(settings), pytest.raises(PluggyCredentialsError):
        _auto_register_providers()

    assert "pluggy" not in _PROVIDERS
