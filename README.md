# Root

A backend microservice that helps identify **likely root causes of incidents** (e.g. production failures, CI issues) by analyzing time-series metrics and discrete system events.

Instead of manually digging through logs and dashboards, the service:
- ingests metrics and events
- detects anomalous behavior
- links anomalies to nearby changes (deploys, flags, config updates)
- ranks likely causes with explainable evidence

This project is built as a production-style API using FastAPI, PostgreSQL, and Alembic.

---

## Current Status

✅ FastAPI application scaffolded  
✅ PostgreSQL database configured  
✅ SQLAlchemy ORM models defined  
✅ Alembic migrations set up and applied  

**Next steps**
- Define API schemas
- Implement `POST /ingest`
- Implement `GET /analysis/{incident_id}`
- Add anomaly detection + scoring logic

---

## Tech Stack

- **Python 3**
- **FastAPI** – API framework
- **PostgreSQL** – persistent storage
- **SQLAlchemy** – ORM
- **Alembic** – schema migrations
- **Pydantic** – request/response validation

---

## Project Structure
