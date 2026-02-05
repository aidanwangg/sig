
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal, engine
from app import models
from app.schemas import IngestRequest, IngestResponse

from collections import defaultdict
from datetime import timedelta
import math

from app.schemas import AnalysisResponse, AnomalyOut, CauseOut


app = FastAPI()


# --- DB session per request ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def startup():
    # check that DB is reachable
    with engine.connect() as conn:
        print("✅ Database connection successful")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse)
def ingest(payload: IngestRequest, db: Session = Depends(get_db)):
    try:
        # 1) Find or create the incident
        incident = None
        if payload.incident_id:
            incident = db.get(models.Incident, payload.incident_id)

        if incident is None:
            incident = models.Incident(
                id=payload.incident_id,  # if None, model default will generate uuid
                name=payload.name,
                source=payload.source,
                meta=payload.meta,
            )
            db.add(incident)
            db.flush()  # ensures incident.id is available

        # 2) Insert metric points
        metric_rows = [
            models.MetricPoint(
                incident_id=incident.id,
                ts=m.ts,
                metric_name=m.metric_name,
                value=m.value,
            )
            for m in payload.metrics
        ]

        # 3) Insert events
        event_rows = [
            models.Event(
                incident_id=incident.id,
                ts=e.ts,
                event_type=e.event_type,
                meta=e.meta,
            )
            for e in payload.events
        ]

        # --- bulk insert metrics with ON CONFLICT DO NOTHING ---
        if metric_rows:
            stmt = insert(models.MetricPoint).values([
                {
                    "incident_id": incident.id,
                    "ts": m.ts,
                    "metric_name": m.metric_name,
                    "value": m.value,
                }
                for m in payload.metrics
            ]).on_conflict_do_nothing(
                index_elements=["incident_id", "ts", "metric_name"]
            )
            result_metrics = db.execute(stmt)
            metrics_inserted = result_metrics.rowcount or 0
        else:
            metrics_inserted = 0

        # --- bulk insert events with ON CONFLICT DO NOTHING ---
        if event_rows:
            stmt = insert(models.Event).values([
                {
                    "incident_id": incident.id,
                    "ts": e.ts,
                    "event_type": e.event_type,
                    "meta": e.meta,
                }
                for e in payload.events
            ]).on_conflict_do_nothing(
                index_elements=["incident_id", "ts", "event_type"]
            )
            result_events = db.execute(stmt)
            events_inserted = result_events.rowcount or 0
        else:
            events_inserted = 0

        db.commit()

        return IngestResponse(
            incident_id=incident.id,
            metrics_ingested=metrics_inserted,
            events_ingested=events_inserted,
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analysis/{incident_id}", response_model=AnalysisResponse)
def analyze_incident(incident_id: str, db: Session = Depends(get_db)):
    # 1) Load incident
    incident = db.get(models.Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    # 2) Load metrics + events for this incident
    metric_points = (
        db.query(models.MetricPoint)
        .filter(models.MetricPoint.incident_id == incident_id)
        .order_by(models.MetricPoint.metric_name, models.MetricPoint.ts)
        .all()
    )

    events = (
        db.query(models.Event)
        .filter(models.Event.incident_id == incident_id)
        .order_by(models.Event.ts)
        .all()
    )

    # Group metrics by name
    by_metric = defaultdict(list)
    for mp in metric_points:
        by_metric[mp.metric_name].append(mp)

    anomalies: list[AnomalyOut] = []

    # 3) Detect anomalies with a simple baseline z-score
    # Baseline = first N points in that metric stream (min 10, max 30)
    for metric_name, pts in by_metric.items():
        if len(pts) < 6:
            continue

        baseline_n = min(30, max(10, len(pts) // 5))
        baseline = pts[:baseline_n]

        mean = sum(p.value for p in baseline) / len(baseline)
        var = sum((p.value - mean) ** 2 for p in baseline) / len(baseline)
        std = math.sqrt(var)

        # If std is ~0, z-score isn't meaningful
        if std < 1e-9:
            continue

        for p in pts[baseline_n:]:
            z = (p.value - mean) / std
            if abs(z) >= 3.0:
                anomalies.append(
                    AnomalyOut(
                        metric_name=metric_name,
                        ts=p.ts,
                        value=p.value,
                        baseline_mean=mean,
                        baseline_std=std,
                        z_score=z,
                    )
                )

    # Sort anomalies chronologically
    anomalies.sort(key=lambda a: a.ts)

    # 4) Link anomalies to nearby events and rank likely causes
    # Window: +/- 5 minutes around anomaly timestamp
    window = timedelta(minutes=5)

    # Count how often each event is "near" anomalies, and compute a proximity score
    cause_stats = {}  # key: event_id -> dict(score, evidence, event_obj)
    for a in anomalies:
        for ev in events:
            if ev.ts is None:
                continue
            dt = abs(a.ts - ev.ts)
            if dt <= window:
                # Simple proximity score: closer => higher
                # dt=0 => 1.0, dt=5min => ~0.0
                proximity = max(0.0, 1.0 - (dt.total_seconds() / window.total_seconds()))

                key = ev.id
                if key not in cause_stats:
                    cause_stats[key] = {
                        "score": 0.0,
                        "evidence": [],
                        "event": ev,
                    }

                cause_stats[key]["score"] += proximity
                cause_stats[key]["evidence"].append(
                    f"{a.metric_name} anomalous at {a.ts.isoformat()} (z={a.z_score:.2f}) near event"
                )

    # Convert to CauseOut list
    causes: list[CauseOut] = []
    if cause_stats:
        # Normalize confidence into [0,1] by dividing by max score
        max_score = max(v["score"] for v in cause_stats.values()) or 1.0

        for v in cause_stats.values():
            ev = v["event"]
            confidence = v["score"] / max_score

            causes.append(
                CauseOut(
                    event_type=ev.event_type,
                    ts=ev.ts,
                    meta=ev.meta,
                    confidence=round(confidence, 3),
                    evidence=v["evidence"][:5],  # keep it short
                )
            )

        causes.sort(key=lambda c: c.confidence, reverse=True)

    return AnalysisResponse(
        incident_id=incident_id,
        anomalies=anomalies,
        likely_causes=causes[:5],
    )
