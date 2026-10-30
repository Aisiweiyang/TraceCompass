"""领域层:内部归一化实体(data-model.md §2)。纯数据定义,无 IO。"""

from psios.domain.conversation import ROLES, Conversation, Message, content_hash

__all__ = ["ROLES", "Conversation", "Message", "content_hash"]
