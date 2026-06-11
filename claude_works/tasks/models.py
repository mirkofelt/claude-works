from dataclasses import dataclass


@dataclass
class IncomingMessage:
    telegram_message_id: int
    chat_id: int
    from_user_id: int
    text: str | None
    voice_file_id: str | None
    timestamp: int
    is_edited: bool = False
