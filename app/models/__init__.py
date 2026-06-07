"""ORM models. Import all here so Alembic autogenerate sees them."""

from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.callback_request import CallbackRequest
from app.models.dentiva_staff import DentivaStaff
from app.models.feature_flag import FeatureFlag
from app.models.invitation import Invitation
from app.models.invoice import Invoice
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.subscription import Subscription
from app.models.usage_record import UsageRecord
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry

__all__ = [
    "Practice",
    "User",
    "Patient",
    "Call",
    "Booking",
    "AuditLog",
    "CallbackRequest",
    "WaitlistEntry",
    "DentivaStaff",
    "Invitation",
    "Subscription",
    "Invoice",
    "UsageRecord",
    "FeatureFlag",
]
