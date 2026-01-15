# System Overview

## 1. What this system is
A production backend that serves a Flutter mobile application via REST APIs.
Feature scale: medium.
Clients: mobile only.

---

## 2. Tech Stack
- Language: Python
- Framework: (FastAPI / Django / Flask)
- API Style: REST
- Auth: (JWT / OAuth / Session)
- Database: (PostgreSQL / MySQL / SQLite)
- ORM: (SQLAlchemy / Django ORM)
- Async: (Yes / No)
- Background jobs: (Celery / None)

---

## 3. Architecture Style
- Monolith / Modular monolith
- Layered structure:
  - Routers (HTTP layer)
  - Services (business logic)
  - Models (DB)
  - Utils / Helpers

---

## 4. API Overview
- Total endpoints: ~100
- Versioning: (/v1 or none)
- Auth scopes:
  - User
  - Admin
- Error format:
```json
{
  "error_code": "string",
  "message": "string"
}