"""The wizard's pairing transaction, testable without the Windows interface."""

from __future__ import annotations

from .device import KoboDevice
from .pairing import PairingClient, PairingCode, PairingError
from .service import KoboPatcher, PatchError, PatchResult


def pair_and_patch(device: KoboDevice, code: PairingCode) -> PatchResult:
    patcher = KoboPatcher(device)
    patcher.current_endpoint()
    client = PairingClient(code)
    session: str | None = None
    try:
        session = client.claim()
        endpoint = client.complete(session, device.label or "Kobo")
        return patcher.patch(endpoint)
    except (PairingError, PatchError, OSError):
        if session and not client.revoke(session):
            raise PatchError(
                "Koppelen is mislukt en intrekken van de nieuwe koppeling kon niet worden bevestigd. "
                "Trek deze koppeling in via je Kobo-instellingen in BookFlow voordat je opnieuw probeert."
            ) from None
        raise
