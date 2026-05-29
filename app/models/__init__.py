"""ORM models. Import all here so Alembic autogenerate sees them."""

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.user import User

__all__ = ["Practice", "User", "Patient", "Call", "Booking", "AuditLog"]
