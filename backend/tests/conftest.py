"""Test configuration and fixtures."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.database import Base, get_db
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models.tenant import Tenant
from app.models.user import User

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"


@compiles(JSONB, "sqlite")
def compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session


@pytest.fixture
async def tenant(db: AsyncSession) -> Tenant:
    tenant = Tenant(name="Test Corp", slug="test-corp")
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@pytest.fixture
async def user(db: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="test@testcorp.com",
        hashed_password=hash_password("testpassword"),
        full_name="Test User",
        role="owner",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
def auth_headers(user: User, tenant: Tenant) -> dict:
    token = create_access_token(user.id, tenant.id, user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
