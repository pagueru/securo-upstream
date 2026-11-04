import uuid

from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectTokenData,
    ConnectionData,
    HoldingData,
    InstitutionData,
    InstitutionListData,
    ProviderUserActionRequired,
    RefreshOutcome,
    SessionExpiredError,
    TransactionData,
)

# Registry of available providers.
_PROVIDERS: dict[str, type[BankProvider]] = {}

# All known providers the system supports (extensible for future connectors).
KNOWN_PROVIDERS = [
    {
        "name": "pluggy",
        "display_name": "Pluggy",
        "description": "Open finance provider for Brazilian banks",
        "flow_type": "widget",
        "requires_institution_select": False,
        "supports_asset_sync": True,
    },
    {
        "name": "enable_banking",
        "display_name": "Enable Banking",
        "description": "European banks via PSD2 open banking",
        "flow_type": "oauth",
        "requires_institution_select": True,
        "supports_asset_sync": False,
    },
    {
        "name": "simplefin",
        "display_name": "SimpleFIN",
        "description": "US and international banks via SimpleFIN Bridge",
        "flow_type": "token",
        "requires_institution_select": False,
        "supports_asset_sync": True,
    },
]


def register_provider(name: str, cls: type[BankProvider]) -> None:
    """Register a bank provider implementation."""
    _PROVIDERS[name] = cls


def get_provider(name: str, workspace_id: uuid.UUID | None = None) -> BankProvider:
    """Get a registered bank provider instance, configured for one workspace."""
    provider_class = _PROVIDERS.get(name)
    if not provider_class:
        available = ", ".join(_PROVIDERS.keys()) or "(none)"
        raise ValueError(f"Unknown provider: {name}. Available: {available}")
    return provider_class.for_workspace(workspace_id)


def list_providers() -> list[dict[str, str]]:
    """Return info about all registered providers."""
    return [
        {"name": name, "flow_type": cls().flow_type}
        for name, cls in _PROVIDERS.items()
    ]


def all_known_providers() -> list[dict]:
    """Return all known providers with a configured flag."""
    return [
        {**p, "configured": p["name"] in _PROVIDERS}
        for p in KNOWN_PROVIDERS
    ]


def _auto_register_providers() -> None:
    """Auto-register providers when credentials are configured."""
    from app.core.config import get_settings
    from app.providers.pluggy_credentials import (
        get_workspace_credentials,
        instance_credentials,
    )

    settings = get_settings()

    # Loading the map eagerly makes a malformed credentials file fail at startup.
    has_instance_credentials = instance_credentials() is not None
    has_workspace_credentials = bool(get_workspace_credentials())
    if has_instance_credentials or has_workspace_credentials:
        from app.providers.pluggy import PluggyProvider
        register_provider("pluggy", PluggyProvider)

    eb_has_key = bool(
        settings.enable_banking_private_key or settings.enable_banking_private_key_file
    )
    if settings.enable_banking_app_id and eb_has_key:
        from app.providers.enable_banking import EnableBankingProvider
        register_provider("enable_banking", EnableBankingProvider)

    if settings.simplefin_enabled:
        from app.providers.simplefin import SimpleFinProvider
        register_provider("simplefin", SimpleFinProvider)


_auto_register_providers()


_storage_provider = None


def get_storage_provider():
    """Get the configured storage provider (singleton)."""
    global _storage_provider
    if _storage_provider is None:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.storage_provider == "local":
            from app.providers.local_storage import LocalStorageProvider

            _storage_provider = LocalStorageProvider()
        else:
            raise NotImplementedError(
                f"Storage provider '{settings.storage_provider}' is not yet implemented. "
                "Supported: 'local'"
            )
    return _storage_provider


__all__ = [
    "BankProvider",
    "AccountData",
    "TransactionData",
    "ConnectionData",
    "ConnectTokenData",
    "HoldingData",
    "InstitutionData",
    "InstitutionListData",
    "ProviderUserActionRequired",
    "RefreshOutcome",
    "SessionExpiredError",
    "register_provider",
    "get_provider",
    "list_providers",
    "all_known_providers",
    "get_storage_provider",
]
