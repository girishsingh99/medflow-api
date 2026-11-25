# MedFlow API

A REST API for clinic and hospital appointments and billing, shared by many tenants (organizations).
Stack: Python 3.12, Django 6.1, Django REST Framework, drf-spectacular, PostgreSQL (psycopg 3),
Celery + Redis, pytest + pytest-django, ruff. Packages are managed with **uv**.

## Commands (always go through `uv run`; never call pip or bare python)

| Task | Command |
|---|---|
| Install/sync deps | `uv sync` |
| Add runtime dep / dev dep | `uv add <pkg>` / `uv add --dev <pkg>` |
| Dev server | `uv run python manage.py runserver` |
| Make / apply migrations | `uv run python manage.py makemigrations <app>` / `uv run python manage.py migrate` |
| Shell | `uv run python manage.py shell` |
| Lint (autofix) | `uv run ruff check . --fix` |
| Format | `uv run ruff format .` |
| CI check (no writes) | `uv run ruff check . && uv run ruff format --check .` |
| Tests | `uv run pytest` |
| Celery worker / beat | `uv run celery -A config worker -l info` / `uv run celery -A config beat -l info` |
| Full stack (Docker) | `docker compose up --build -d` (db, redis, web, worker, beat) |
| Demo data | `uv run python manage.py seed_demo_data` (needs DEBUG=True or `--force`; idempotent) |
| Validate OpenAPI | `uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null` |

Commit `uv.lock` whenever deps change. Don't edit it by hand.

## Configuration
Settings read env vars (or `.env`, see `.env.example`) via django-environ.
`DATABASE_URL=postgres://...` selects PostgreSQL; when unset it falls back to `db.sqlite3`.
SQLite is for quick local runs only. The double-booking `ExclusionConstraint` (and `btree_gist`) is applied
only on Postgres through `apps.common.migration_operations.AddPostgresOnlyConstraint`. If you regenerate the
appointments migration, keep that hand edit.

## Layout

```
config/            settings, root urls, asgi/wsgi, celery app
apps/
  common/          BaseModel/TenantModel (cross-tenant FK guard), migration ops, test helpers (no concrete models)
  users/           custom User (email login), auth endpoints
  clinics/         Organization (tenant), Membership/roles, Clinic, Provider, schedules, Patient
  appointments/    Appointment, status history, booking/availability services, reminder tasks
  billing/         ServiceItem catalog, Invoice, InvoiceLineItem, Payment, invoice numbering, summary task
  web/             operations UI (session auth, templates + Tailwind/HTMX/Alpine via CDN); consumer only
  demo/            seed_demo_data command only (top of the dependency chain)
src/medflow_api/   unused uv stub; will be removed
conftest.py        root pytest fixtures (org, other_org)
```

Each app follows this shape:

```
apps/<app>/
  models.py  admin.py  apps.py
  serializers.py   # validation and representation only
  services.py      # business logic and writes (transactions live here)
  selectors.py     # read/query helpers
  views.py  urls.py  permissions.py  tasks.py (Celery)
  migrations/
  tests/  conftest.py  factories.py  test_*.py
```

Dependencies only point one way: `users` ← `clinics` ← `appointments` ← `billing` ← `web`/`demo`. Never import the other way.
`AUTH_USER_MODEL = "users.User"` has to be set before the first migration.

## Conventions

- **Multi-tenancy** (shared schema): every tenant-owned model inherits `TenantModel` (an `organization` FK).
  Never query a tenant model without scoping it. Use `Model.objects.for_org(org)` or the
  `TenantViewSetMixin` (`apps/common/api/views.py`), which takes the org from the authenticated user's `Membership`.
  Cross-tenant access returns 404, not 403.
- Related rows must belong to the same organization (for example, an appointment's clinic, provider and patient).
  Services check this, and model `clean()` repeats the check.
- Primary keys are UUIDs (`id = UUIDField(default=uuid4)`). Every model has `created_at`/`updated_at`.
- Money is `DecimalField(max_digits=12, decimal_places=2)`. Never use float. Currency is set on the Organization.
- Datetimes are timezone-aware (`USE_TZ=True`) and stored in UTC. The clinic's timezone is used only for display and scheduling.
- Status fields use `models.TextChoices`. State changes go through service functions, never `obj.status = ...` in views.
- Fat services, thin views and serializers. Wrap multi-row writes in `transaction.atomic()`.
- Celery tasks take IDs, not model instances, and must be idempotent: lock the row and check or stamp a marker field.
  Enqueue from services with `transaction.on_commit(lambda: task.delay(id))`, never mid-transaction.
  Give tasks explicit `name="<app>.<task>"`. Periodic tasks go in `CELERY_BEAT_SCHEDULE` in settings.
- Patient data is PHI. Never log it, print it, or put it in fixtures with realistic values.

## API (DRF + SimpleJWT + drf-spectacular)
- Everything lives under `/api/v1/`. Docs are at `/api/docs/` (Swagger) and `/api/redoc/`; the schema is at `/api/schema/`.
  Check the schema with `uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null`.
- Auth: `POST /api/v1/auth/token/` with `{email, password}` returns access and refresh tokens. Send `Authorization: Bearer <access>`.
- Organization: `OrgRolePermission` resolves `request.membership` and `request.organization`. It uses the
  `X-Organization: <slug>` header, which is required only for users who belong to more than one organization.
- Roles: `ORG_ADMIN`, `DOCTOR`, `RECEPTIONIST`, `BILLING_STAFF`. Every tenant viewset declares
  `role_map = build_role_map(read=..., write=..., <custom_action>=roles | {"GET": .., "POST": ..})`.
  Actions missing from the map are **denied**. The allowed roles appear automatically in the Swagger description.
- Tenant viewsets subclass `TenantCRUDViewSet` (list/retrieve/create/PATCH, no DELETE) or `TenantViewSetMixin`.
- Every FK accepted in a request body uses `TenantRelatedField`, so another org's id gets a 400 "does not exist".
  Per-org uniqueness is declared through `TenantModelSerializer.tenant_unique_fields`.
- Error contract:
  - 401: not authenticated.
  - 403: no membership, or the role isn't allowed.
  - 404: the object belongs to another org.
  - 400: validation error.
  - 409: domain conflict (double booking, invalid state, duplicate).
- Services raise `apps.common.exceptions.DomainError` subclasses (`status_code`, `code`) and never import DRF.
  `apps/common/api/exceptions.py` maps them to responses as `{"detail", "code"}`.
- `ATOMIC_REQUESTS=True`: each request is one transaction.
- Payments require an `Idempotency-Key` header. A replay returns 200 with `Idempotent-Replayed: true`.

## Web UI (`apps/web`)
- Pages live under `/dashboard/`; login is at `/login/`. The UI uses **session** auth, while the API stays JWT-only.
- Role sets come from `apps/<app>/policies.py`, which the API viewsets share. Change access there, never in the view.
- Decorate every view with `@tenant_view(read_roles, write_roles=...)` (`apps/web/tenancy.py`). Query through
  `for_org(request.organization)`, and scope appointments with `scope_for_membership` (doctors see only their own).
- Writes call the domain services. On `DomainError`, show the message inline or as an error toast.
- HTMX conventions: return a partial when `wants_partial(request, target)`, then signal with
  `hx_trigger(response, ...)` / `toast(response, msg)`. Event names are kebab-case (`kpis-changed`, `close-modal`).
  Lists use `id="results"` and refresh on their `*-changed` event.
- CDN scripts are pinned with SRI hashes in `templates/web/base.html`. Update the version and hash together.
- Tests go in `apps/web/tests/`, using the `web_for(role, org)`, `admin_web` and `htmx(target)` helpers. Every view gets a tenant-isolation test in `test_tenancy.py`.

## Ruff

Config lives in `pyproject.toml`. Settings: line length 100, double quotes, target py312, migrations excluded.
Rule sets: E, W, F, I (isort), B, UP, DJ (django), SIM, C4, RUF, PT, N, S (bandit).
`S101`, `S105` and `S106` are allowed in tests. `RUF012` is ignored project-wide (Django `Meta` and admin lists). First-party packages for isort: `apps`, `config`.
Run `uv run ruff check . --fix` and `uv run ruff format .` before finishing any task.

## Testing (pytest + pytest-django)

- Config: `[tool.pytest.ini_options]` sets `DJANGO_SETTINGS_MODULE=config.settings`, `testpaths=["apps"]`, `--reuse-db`.
- Tests sit next to their code in `apps/<app>/tests/test_*.py`. Shared fixtures go in `conftest.py` at the root or per app.
- Use the `db` fixture or the `@pytest.mark.django_db` marker. Shared fixtures `org` and `other_org` live in the root `conftest.py`.
- Build test data with the factory-boy factories in `apps/<app>/tests/factories.py`. Pass `organization=` explicitly; sub-factories inherit it.
- Celery runs eagerly in all tests (an autouse fixture in the root `conftest.py`), so no Redis is needed. To assert that a task was
  *enqueued*, monkeypatch `task.delay`. To run on-commit hooks, use the `django_capture_on_commit_callbacks` fixture.
  Check emails with `mailoutbox`.
- Mark tests that need Postgres with `apps.common.testing.requires_postgres`. They are skipped on the SQLite fallback.
- Every endpoint test also checks **tenant isolation**: a user from org A must not see org B's rows.
  Cross-cutting API isolation tests live in `apps/common/tests/test_api_tenancy.py`.
- API clients: use the `client_for(role, org)`, `admin_client`, `receptionist_client` and `billing_client` fixtures, or
  `apps.common.testing.authed_client(user, org_slug=None)`. Tests use the MD5 password hasher for speed.
- Tests that rely on Postgres features (exclusion constraints, `select_for_update`) need a Postgres test DB, not SQLite.
- Useful commands:
  - `uv run pytest apps/billing` runs one app
  - `uv run pytest -k invoice -x` filters by name and stops at the first failure
  - `uv run pytest --create-db` rebuilds the test DB after migration changes
  - `uv run pytest --lf` reruns only the last failures
- Workflow: write or adjust the test, implement, run `uv run pytest`, run ruff, then commit.
