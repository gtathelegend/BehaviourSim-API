"""Shared pytest fixtures for BehaviorSim API tests."""

from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.session import get_db
from app.main import app

# In-memory SQLite engine for fast, isolated test execution
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create all database tables once for the test session and seed free plan."""
    Base.metadata.create_all(bind=test_engine)
    with TestingSessionLocal() as session:
        from app.services.plan import get_or_create_free_plan

        get_or_create_free_plan(session)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated database session rolled back after every test."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)

    yield session

    session.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Create a TestClient instance with request-scoped database dependency overridden."""
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
