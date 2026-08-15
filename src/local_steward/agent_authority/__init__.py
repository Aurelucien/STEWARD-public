"""Host-owned risk authority for the native STEWARD Agent surface."""

from .errors import (
    StewardAuthorityContextError,
    StewardAuthorityError,
    StewardAuthorityRequiredError,
)
from .models import (
    AUTHORITY_SCHEMA_NAME,
    AUTHORITY_SCHEMA_VERSION,
    AuthorityAdmission,
    AuthorityContext,
    AuthorityPath,
    AuthoritySource,
    RiskClass,
)
from .service import (
    authority_context_machine_object,
    create_authority_context,
    load_authority_context,
    require_authority,
)

__all__ = [
    "AUTHORITY_SCHEMA_NAME",
    "AUTHORITY_SCHEMA_VERSION",
    "AuthorityAdmission",
    "AuthorityContext",
    "AuthorityPath",
    "AuthoritySource",
    "RiskClass",
    "StewardAuthorityContextError",
    "StewardAuthorityError",
    "StewardAuthorityRequiredError",
    "authority_context_machine_object",
    "create_authority_context",
    "load_authority_context",
    "require_authority",
]
