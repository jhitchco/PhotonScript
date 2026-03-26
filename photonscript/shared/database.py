"""SQLAlchemy async database setup and table definitions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime, Enum,
    create_engine, event,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Session

from photonscript.shared.models import FilterType, ImageStatus, TargetTier


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM Tables
# ---------------------------------------------------------------------------

class ProjectRow(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    target_json = Column(Text, nullable=False)  # serialized CelestialTarget
    exposure_plans_json = Column(Text, nullable=False)
    priority = Column(Integer, default=50)
    total_integration_hours = Column(Float, default=0.0)
    completion_pct = Column(Float, default=0.0)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ImageRow(Base):
    __tablename__ = "images"

    id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    file_size_bytes = Column(Integer, default=0)
    target_name = Column(String, nullable=False)
    filter_type = Column(String, nullable=False)
    exposure_seconds = Column(Float, nullable=False)
    gain = Column(Integer, default=100)
    offset = Column(Integer, default=50)
    binning = Column(Integer, default=1)
    captured_at = Column(DateTime, default=datetime.utcnow)
    camera_temp_c = Column(Float, nullable=True)
    status = Column(String, default=ImageStatus.CAPTURED.value)
    quality_json = Column(Text, default="{}")
    transferred = Column(Boolean, default=False)
    transferred_at = Column(DateTime, nullable=True)
    local_path = Column(String, nullable=True)


class TransferRow(Base):
    __tablename__ = "transfers"

    id = Column(String, primary_key=True)
    image_id = Column(String, nullable=False, index=True)
    source_path = Column(String, nullable=False)
    dest_path = Column(String, nullable=False)
    file_size_bytes = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    transfer_rate_mbps = Column(Float, nullable=True)
    status = Column(String, default="pending")
    error = Column(Text, default="")


class NightLogRow(Base):
    __tablename__ = "night_logs"

    id = Column(String, primary_key=True)
    date = Column(String, nullable=False, index=True)  # YYYY-MM-DD
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    targets_observed = Column(Text, default="[]")  # JSON list
    images_captured = Column(Integer, default=0)
    images_rejected = Column(Integer, default=0)
    total_exposure_seconds = Column(Float, default=0.0)
    avg_fwhm = Column(Float, nullable=True)
    avg_guiding_rms = Column(Float, nullable=True)
    notes = Column(Text, default="")


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------

async def get_async_engine(db_path: str | Path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def get_async_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
