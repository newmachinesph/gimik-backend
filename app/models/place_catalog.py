"""Reusable, provenance-backed places and private search demand.

Google predictions never enter these tables. An AI match is an activity-fit
assessment, not proof of access, safety, opening hours, or a meeting point.
"""

import datetime as dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedMixin, IdMixin


class CatalogPlace(Base, IdMixin, CreatedMixin):
    __tablename__ = "catalog_places"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    # This references the common plan-compatible place identity.
    place_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"), unique=True
    )
    source: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(500))
    license: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    # Import alone never approves public recommendations.
    reviewed: Mapped[bool] = mapped_column(default=False)
    coordinate_kind: Mapped[str] = mapped_column(String(30), default="general_location")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class PlaceAssessment(Base, IdMixin, CreatedMixin):
    __tablename__ = "place_assessments"
    __table_args__ = (
        UniqueConstraint("catalog_place_id", "someday_id", "evidence_hash"),
        Index("ix_place_assessments_queue", "status", "available_at"),
    )

    catalog_place_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_places.id", ondelete="CASCADE")
    )
    someday_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("somedays.id", ondelete="CASCADE"))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(String(600), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlaceSearchEvent(Base, IdMixin, CreatedMixin):
    __tablename__ = "place_search_events"
    __table_args__ = (Index("ix_place_search_events_created_at", "created_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # User-authored query only. Never persist a provider's result text here.
    query: Mapped[str] = mapped_column(String(120))
    someday_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("somedays.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(30), default="started")
    result_count: Mapped[int] = mapped_column(default=0)

