"""Project-wide pytest fixtures."""

import pytest
from rest_framework.test import APIClient

from apps.clinics.models import MembershipRole
from apps.clinics.tests.factories import MembershipFactory, OrganizationFactory
from apps.common.testing import authed_client
from apps.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    """PBKDF2 is deliberately slow; tests don't need it."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _celery_eager():
    """Run Celery tasks inline so no test ever needs a Redis broker.

    Tests that check *enqueueing* mock ``.delay`` instead.
    """
    from config.celery import app

    app.conf.update(task_always_eager=True, task_eager_propagates=True)
    yield app
    app.conf.update(task_always_eager=False, task_eager_propagates=False)


@pytest.fixture
def org(db):
    return OrganizationFactory(name="Org A", slug="org-a")


@pytest.fixture
def other_org(db):
    return OrganizationFactory(name="Org B", slug="org-b")


@pytest.fixture
def api_client():
    """Unauthenticated client."""
    return APIClient()


@pytest.fixture
def client_for(db):
    """``client_for(role, org)`` -> APIClient for a new member with that role.

    The returned client exposes ``.user`` and ``.membership``.
    """

    def make(role, organization, *, user=None):
        user = user or UserFactory()
        membership = MembershipFactory(user=user, organization=organization, role=role)
        client = authed_client(user)
        client.membership = membership
        return client

    return make


@pytest.fixture
def admin_client(client_for, org):
    return client_for(MembershipRole.ORG_ADMIN, org)


@pytest.fixture
def receptionist_client(client_for, org):
    return client_for(MembershipRole.RECEPTIONIST, org)


@pytest.fixture
def billing_client(client_for, org):
    return client_for(MembershipRole.BILLING_STAFF, org)
