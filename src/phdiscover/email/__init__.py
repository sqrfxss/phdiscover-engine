"""PhDiscover Email Package — zero-cost newsletter delivery."""
from phdiscover.email.service import (
    BrevoProvider,
    EmailProvider,
    EmailResult,
    EmailService,
    ResendProvider,
    SMTPProvider,
    Subscriber,
    SubscriberStore,
    render_position_newsletter,
    render_welcome_email,
)

__all__ = [
    "BrevoProvider",
    "EmailProvider",
    "EmailResult",
    "EmailService",
    "ResendProvider",
    "SMTPProvider",
    "Subscriber",
    "SubscriberStore",
    "render_position_newsletter",
    "render_welcome_email",
]
