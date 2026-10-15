"""Permission-enforcing local WeChat and QQ read interface."""

from .service import AccessError, AccessService, Conversation, Page, PolicyStore

__all__ = ["AccessError", "AccessService", "Conversation", "Page", "PolicyStore"]
