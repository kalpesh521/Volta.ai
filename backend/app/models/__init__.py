"""
Import all models here so Alembic's autogenerate and SQLAlchemy's mapper
configuration can discover every table via `Base.metadata`.
"""
from app.models.auth_provider import AuthProvider  # noqa: F401
from app.models.password_reset_token import PasswordResetToken  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.user import User  # noqa: F401

# Onboarding models — must be imported so Alembic sees these tables
from app.modules.onboarding.models import BatteryConfig  # noqa: F401
from app.modules.onboarding.models import GridConfig  # noqa: F401
from app.modules.onboarding.models import SolarSystem  # noqa: F401
from app.modules.onboarding.models import TrackedAppliance  # noqa: F401

from app.agents_platform.agents.device_action.models import ActionAuditEvent  # noqa: F401
from app.agents_platform.agents.device_action.models import ActionProposal  # noqa: F401
from app.modules.assistant.ops.models import AutomationRule  # noqa: F401
from app.agents_platform.agents.device_action.models import DeviceOverride  # noqa: F401
from app.modules.assistant.notifications.models import HomeNotification  # noqa: F401
from app.modules.knowledge.models import BillExtraction  # noqa: F401
from app.modules.knowledge.models import KnowledgeChunk  # noqa: F401
from app.modules.knowledge.models import KnowledgeDocument  # noqa: F401
from app.modules.knowledge.models import KnowledgeReview  # noqa: F401

from app.agents_platform.conversations import AssistantConversation  # noqa: F401
from app.agents_platform.memory_store.models import UserMemoryItem  # noqa: F401
from app.agents_platform.automation.models import AutomationRuleDef  # noqa: F401
from app.agents_platform.agents.device_action.models import DeviceCommandOutbox  # noqa: F401
