"""Every connection flow resolves the provider for the right workspace."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.models.workspace import Workspace, WorkspaceMember
from app.providers.pluggy_credentials import PluggyCredentialsError
from app.services import connection_service
from app.tasks import sync_tasks


class _SessionContext:
    """Context manager that hands back one shared session."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _EngineStub:
    """Engine stand-in: `_sync_all` only disposes it."""

    async def dispose(self) -> None:
        """No-op; the tests own no real engine."""


def _make_connection(user_id: uuid.UUID, workspace_id: uuid.UUID, index: int) -> BankConnection:
    """Build an active bank connection ready for sync."""
    return BankConnection(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        provider="pluggy",
        external_id=f"item-{index}",
        institution_name="Banco Teste",
        credentials={"item_id": f"item-{index}"},
        status="active",
        last_sync_at=None,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_create_connect_token_resolves_the_provider_for_the_workspace(
    test_workspace,
):
    provider = MagicMock()
    provider.create_connect_token = AsyncMock(return_value=MagicMock(access_token="token"))

    with patch(
        "app.services.connection_service.get_provider", return_value=provider
    ) as mock_get_provider:
        token = await connection_service.create_connect_token(
            "pluggy", uuid.uuid4(), workspace_id=test_workspace.id
        )

    assert token == {"access_token": "token"}
    mock_get_provider.assert_called_once_with("pluggy", workspace_id=test_workspace.id)


@pytest.mark.asyncio
async def test_sync_connection_resolves_the_provider_for_the_connection_workspace(
    session: AsyncSession, test_user, test_workspace
):
    connection = _make_connection(test_user.id, test_workspace.id, 0)
    session.add(connection)
    await session.commit()

    # Stop right after provider resolution: the assertion is about the call.
    with patch(
        "app.services.connection_service.get_provider",
        side_effect=RuntimeError("stop after resolution"),
    ) as mock_get_provider:
        with pytest.raises(RuntimeError):
            await connection_service.sync_connection(
                session, connection.id, test_workspace.id, test_user.id
            )

    mock_get_provider.assert_called_once_with("pluggy", workspace_id=test_workspace.id)


@pytest.mark.asyncio
async def test_sync_all_resolves_each_connection_with_its_own_workspace(
    session: AsyncSession, test_user, test_workspace
):
    other_workspace = Workspace(
        id=uuid.uuid4(),
        name="Casa",
        kind="shared",
        created_by_user_id=test_user.id,
        default_currency="BRL",
        locale="pt-BR",
    )
    session.add(other_workspace)
    await session.flush()
    session.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=other_workspace.id,
            user_id=test_user.id,
            role="owner",
        )
    )
    for index, workspace in enumerate((test_workspace, other_workspace)):
        session.add(_make_connection(test_user.id, workspace.id, index))
    await session.commit()

    def _session_maker() -> _SessionContext:
        return _SessionContext(session)

    # One workspace without Pluggy credentials must not abort the loop for
    # the other connections; the provider only needs to be asked per workspace.
    with (
        patch(
            "app.tasks.sync_tasks._make_session_maker",
            return_value=(_EngineStub(), _session_maker),
        ),
        patch(
            "app.services.connection_service.get_provider",
            side_effect=PluggyCredentialsError("workspace has no Pluggy credentials"),
        ) as mock_get_provider,
    ):
        synced = await sync_tasks._sync_all()

    assert synced == 0
    assert sorted(
        call.kwargs["workspace_id"] for call in mock_get_provider.call_args_list
    ) == sorted([test_workspace.id, other_workspace.id])


@pytest.mark.asyncio
async def test_connect_token_route_passes_the_request_workspace(
    client: AsyncClient, auth_headers, test_workspace
):
    provider = MagicMock()
    provider.create_connect_token = AsyncMock(return_value=MagicMock(access_token="token"))

    with patch(
        "app.services.connection_service.get_provider", return_value=provider
    ) as mock_get_provider:
        response = await client.post(
            "/api/connections/connect-token",
            json={"provider": "pluggy"},
            headers=auth_headers,
        )

    assert response.status_code == 200
    mock_get_provider.assert_called_once_with("pluggy", workspace_id=test_workspace.id)


@pytest.mark.asyncio
async def test_connect_token_route_reports_credentials_error_as_bad_request(
    client: AsyncClient, auth_headers
):
    with patch(
        "app.services.connection_service.get_provider",
        side_effect=PluggyCredentialsError("workspace has no Pluggy credentials"),
    ):
        response = await client.post(
            "/api/connections/connect-token",
            json={"provider": "pluggy"},
            headers=auth_headers,
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_reconnect_token_route_passes_the_request_workspace(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, test_workspace
):
    connection = _make_connection(test_user.id, test_workspace.id, 3)
    session.add(connection)
    await session.commit()

    provider = MagicMock()
    provider.create_connect_token = AsyncMock(return_value=MagicMock(access_token="token"))

    with patch(
        "app.services.connection_service.get_provider", return_value=provider
    ) as mock_get_provider:
        response = await client.post(
            f"/api/connections/{connection.id}/reconnect-token", headers=auth_headers
        )

    assert response.status_code == 200
    mock_get_provider.assert_called_once_with("pluggy", workspace_id=test_workspace.id)


@pytest.mark.asyncio
async def test_reconnect_token_route_reports_credentials_error_as_bad_request(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, test_workspace
):
    connection = _make_connection(test_user.id, test_workspace.id, 4)
    session.add(connection)
    await session.commit()

    with patch(
        "app.services.connection_service.get_provider",
        side_effect=PluggyCredentialsError("workspace has no Pluggy credentials"),
    ):
        response = await client.post(
            f"/api/connections/{connection.id}/reconnect-token", headers=auth_headers
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_institutions_route_passes_the_request_workspace(
    client: AsyncClient, auth_headers, test_workspace
):
    provider = MagicMock()
    provider.list_institutions = AsyncMock(return_value=MagicMock(countries=[], institutions=[]))

    with patch(
        "app.services.connection_service.get_provider", return_value=provider
    ) as mock_get_provider:
        response = await client.get("/api/connections/pluggy/institutions", headers=auth_headers)

    assert response.status_code == 200
    mock_get_provider.assert_called_once_with("pluggy", workspace_id=test_workspace.id)
