"""Document types and who is allowed to upload them.

Shared documents are the same for every home and are published on admin upload.
Private documents belong to one user and are retrieved only for that user.
"""
from enum import StrEnum


class Scope(StrEnum):
    SHARED = "shared"
    PRIVATE = "private"


class DocType(StrEnum):
    INVERTER_MANUAL = "inverter_manual"
    BATTERY_MANUAL = "battery_manual"
    NET_METERING_POLICY = "net_metering_policy"
    TARIFF = "tariff"
    HELP = "help"
    ELECTRICITY_BILL = "electricity_bill"
    INSTALLATION_WARRANTY = "installation_warranty"


class UserDocType(StrEnum):
    ELECTRICITY_BILL = "electricity_bill"
    INSTALLATION_WARRANTY = "installation_warranty"


class AdminDocType(StrEnum):
    INVERTER_MANUAL = "inverter_manual"
    BATTERY_MANUAL = "battery_manual"
    NET_METERING_POLICY = "net_metering_policy"
    TARIFF = "tariff"
    HELP = "help"


class ReviewStatus(StrEnum):
    PENDING = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


ADMIN_DOC_TYPES = frozenset(
    {
        DocType.INVERTER_MANUAL,
        DocType.BATTERY_MANUAL,
        DocType.NET_METERING_POLICY,
        DocType.TARIFF,
        DocType.HELP,
    }
)
USER_DOC_TYPES = frozenset(
    {
        DocType.ELECTRICITY_BILL,
        DocType.INSTALLATION_WARRANTY,
    }
)

# Publishing one of these replaces the previous live copy with the same
# brand / DISCOM so an old tariff cannot stay in retrieval.
AUTO_SUPERSEDE = frozenset(
    {
        DocType.INVERTER_MANUAL,
        DocType.BATTERY_MANUAL,
        DocType.NET_METERING_POLICY,
        DocType.TARIFF,
    }
)


def scope_for(doc_type: DocType) -> Scope:
    if doc_type in ADMIN_DOC_TYPES:
        return Scope.SHARED
    return Scope.PRIVATE
