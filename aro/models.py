"""PostgreSQL tables.

hospitals         real hospitals from OpenStreetMap (+ simulated facilities / beds)
ambulances        simulated fleet: position, type (ALS/BLS), status
emergency_types   accident, heart, burn ... and the hospital facilities each one needs
emergencies       every reported case (patient, location, type, severity)
dispatches        the optimised decision for a case + both routes + how it was computed
road_conditions   road closures and congestion zones entered by the operator
settings          small key/value settings shared by all server instances (traffic level)
osm_cache         the downloaded OpenStreetMap data (compressed), so it is fetched only once
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat() if dt else None


class Hospital(Base):
    __tablename__ = "hospitals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(60), nullable=True)
    osm_id: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    facilities: Mapped[list] = mapped_column(JSON, default=list)   # e.g. ["emergency", "trauma", "cardiac"]
    beds_available: Mapped[int] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="osm")  # osm | seed | manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "lat": self.lat, "lon": self.lon, "address": self.address,
                "phone": self.phone, "osm_id": self.osm_id, "facilities": list(self.facilities or []),
                "beds_available": self.beds_available, "is_active": self.is_active, "source": self.source}


class Ambulance(Base):
    __tablename__ = "ambulances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    kind: Mapped[str] = mapped_column(String(3), default="BLS")      # ALS = advanced, BLS = basic life support
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    base_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="available")  # available | busy | maintenance
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    def to_dict(self):
        return {"id": self.id, "code": self.code, "kind": self.kind, "lat": self.lat, "lon": self.lon,
                "base_name": self.base_name, "status": self.status, "updated_at": iso(self.updated_at)}


class EmergencyType(Base):
    __tablename__ = "emergency_types"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200), default="")
    required_facilities: Mapped[list] = mapped_column(JSON, default=list)
    needs_als: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self):
        return {"code": self.code, "label": self.label, "description": self.description,
                "required_facilities": list(self.required_facilities or []), "needs_als": self.needs_als}


class Emergency(Base):
    __tablename__ = "emergencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    emergency_type: Mapped[str] = mapped_column(ForeignKey("emergency_types.code"))
    severity: Mapped[str] = mapped_column(String(20), default="serious")   # critical | serious | stable
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    type: Mapped[EmergencyType] = relationship()

    def to_dict(self):
        return {"id": self.id, "patient_name": self.patient_name, "phone": self.phone, "address": self.address,
                "lat": self.lat, "lon": self.lon, "emergency_type": self.emergency_type,
                "emergency_label": self.type.label if self.type else self.emergency_type,
                "severity": self.severity, "notes": self.notes, "created_at": iso(self.created_at)}


class Dispatch(Base):
    __tablename__ = "dispatches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    emergency_id: Mapped[int] = mapped_column(ForeignKey("emergencies.id"))
    ambulance_id: Mapped[int | None] = mapped_column(ForeignKey("ambulances.id"), nullable=True)
    hospital_id: Mapped[int | None] = mapped_column(ForeignKey("hospitals.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned | dispatched | completed | cancelled

    eta_to_patient_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_to_patient_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta_to_hospital_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_to_hospital_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_eta_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    route_to_patient: Mapped[list] = mapped_column(JSON, default=list)    # [[lat, lon], ...]
    route_to_hospital: Mapped[list] = mapped_column(JSON, default=list)
    details: Mapped[dict] = mapped_column(JSON, default=dict)             # candidate tables, algorithm stats, warnings

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    emergency: Mapped[Emergency] = relationship()
    ambulance: Mapped[Ambulance | None] = relationship()
    hospital: Mapped[Hospital | None] = relationship()

    def to_dict(self, routes: bool = True):
        d = {"id": self.id, "status": self.status,
             "emergency": self.emergency.to_dict() if self.emergency else None,
             "ambulance": self.ambulance.to_dict() if self.ambulance else None,
             "hospital": self.hospital.to_dict() if self.hospital else None,
             "eta_to_patient_s": self.eta_to_patient_s, "distance_to_patient_m": self.distance_to_patient_m,
             "eta_to_hospital_s": self.eta_to_hospital_s, "distance_to_hospital_m": self.distance_to_hospital_m,
             "total_eta_s": self.total_eta_s, "total_distance_m": self.total_distance_m,
             "created_at": iso(self.created_at), "dispatched_at": iso(self.dispatched_at),
             "completed_at": iso(self.completed_at)}
        if routes:
            d["route_to_patient"] = self.route_to_patient
            d["route_to_hospital"] = self.route_to_hospital
            d["details"] = self.details
        return d


class RoadCondition(Base):
    __tablename__ = "road_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))            # closure | congestion
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[float] = mapped_column(Float, default=30)
    factor: Mapped[float] = mapped_column(Float, default=1.0)  # travel-time multiplier for congestion
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "lat": self.lat, "lon": self.lon, "radius_m": self.radius_m,
                "factor": self.factor, "note": self.note, "active": self.active, "created_at": iso(self.created_at)}


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class OsmCache(Base):
    __tablename__ = "osm_cache"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)          # gzip-compressed JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
