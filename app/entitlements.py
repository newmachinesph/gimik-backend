"""What a free account gets, and the gate that enforces it.

One module rather than constants scattered through the routers, because the
answer to "what does free actually include" should be readable in one screen -
by whoever writes the paywall copy as much as by whoever writes the endpoint.

Every limit here maps to a real marginal cost:

- place search      -> Google Maps Platform, billed per request, the dearest
                       call in the stack by a wide margin
- photos            -> Cloudflare R2 storage
- outbound invites  -> Ably fan-out, notification mail, and the spam surface
- creating groups   -> Ably channels that live as long as the group does

Receiving is never limited. A free account can accept every invite it is sent,
read every thread and join every group, because a network whose free half
cannot participate stops being a network - and the paid half has nobody left
to make plans with.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.deps import current_user
from app.models import User

# Photos on the profile. Four is enough to read someone as a real person, which
# is the job; eight is the indulgence.
FREE_PHOTOS = 4
PLUS_PHOTOS = 8

# Outbound invites per rolling week. The sharpest lever in the product: it is
# the cost driver, the anti-spam ceiling and the paywall all at once, and it
# never stops anyone being invited.
FREE_INVITES_PER_WEEK = 5

# Groups a free account may create. Joining is unlimited.
FREE_GROUPS_CREATED = 0


def photo_allowance(user: User) -> int:
    return PLUS_PHOTOS if user.is_plus else FREE_PHOTOS


class PlusRequired(HTTPException):
    """402 rather than 403.

    403 says "not for you"; 402 says "not yet, and here is what changes it".
    The app keys its paywall sheet off this status, so the distinction is load
    bearing rather than pedantic.
    """

    def __init__(self, feature: str) -> None:
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"{feature} is part of Gora Plus.",
        )


def requires_plus(feature: str):
    """A dependency that refuses the request unless the account is entitled.

    Server-side on purpose. The client knows the tier too and hides the button,
    but a hidden button is a suggestion - the request it would have sent still
    reaches this process, and the Google Maps bill for serving it arrives here.
    """

    async def dependency(user: User = Depends(current_user)) -> User:
        if not user.is_plus:
            raise PlusRequired(feature)
        return user

    return dependency
