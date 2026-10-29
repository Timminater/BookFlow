"""Kobo volume discovery with a small injectable filesystem boundary."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

KoboConfRelative = Path(".kobo") / "Kobo" / "Kobo eReader.conf"
KoboDbRelative = Path(".kobo") / "KoboReader.sqlite"
KoboVersionRelative = Path(".kobo") / "version"

_MODEL_BY_PLATFORM = {
    "N613": "Glo",
    "N236": "Clara HD",
    "N249": "Forma",
    "N437": "Sage",
    "N506": "Elipsa",
    "N605": "Clara 2E",
    "N705": "Aura ONE",
    "N782": "Libra H2O",
    "N873": "Libra 2",
    "N905": "Touch",
}
_FIRMWARE = re.compile(r"\b\d+\.\d+\.\d+(?:\.\d+)?\b")
_PLATFORM = re.compile(r"\bN\d{3,4}\b", re.IGNORECASE)


@dataclass(frozen=True)
class KoboDevice:
    root: Path
    label: str
    firmware: str | None
    platform: str | None
    serial_masked: str | None

    @property
    def config_path(self) -> Path:
        return self.root / KoboConfRelative

    @property
    def display_name(self) -> str:
        return f"{self.root.drive or str(self.root)} — {self.label or 'Kobo'}"


def _mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 4:
        return "••••"
    return f"{value[:2]}{'•' * max(4, len(value) - 4)}{value[-2:]}"


def _version_details(version_path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        text = version_path.read_text(encoding="utf-8", errors="replace")[:16_384]
    except OSError:
        return None, None, None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([^=:\s]+)\s*[=:]\s*(.+?)\s*$", line)
        if match:
            fields[match.group(1).casefold()] = match.group(2)
    tokens = [token.strip() for token in re.split(r"[,;\r\n]+", text) if token.strip()]
    firmware = fields.get("firmware") or fields.get("version")
    if not firmware:
        firmware_match = _FIRMWARE.search(text)
        firmware = firmware_match.group(0) if firmware_match else None
    platform = fields.get("platform") or fields.get("model")
    if not platform:
        platform_match = next((match for token in tokens if (match := _PLATFORM.search(token))), None)
        platform = platform_match.group(0).upper() if platform_match else None
    if platform:
        normalized_platform = platform.strip().upper()
        platform = _MODEL_BY_PLATFORM.get(normalized_platform, normalized_platform)
    serial = fields.get("serialnumber") or fields.get("serial")
    if not serial:
        serial_match = re.search(r"(?:^|[,;\r\n])\s*serial(?:number)?\s*[=:]\s*([^,;\r\n]+)", text, re.IGNORECASE)
        serial = serial_match.group(1) if serial_match else None
    # Some firmware uses comma-separated values. Do not expose opaque values as a serial.
    return firmware, platform, _mask(serial) if serial else None


def _kernel32() -> object | None:
    if not hasattr(ctypes, "windll"):
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLogicalDrives.argtypes = []
    kernel32.GetLogicalDrives.restype = wintypes.DWORD
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class WindowsVolumeScanner:
    """Uses Win32 only for roots/labels; detection remains filesystem-testable."""

    def mounted_roots(self) -> list[Path]:
        kernel32 = _kernel32()
        if kernel32 is None:
            return []
        mask = kernel32.GetLogicalDrives()
        return [
            Path(f"{chr(code)}:\\")
            for code in range(ord("A"), ord("Z") + 1)
            if mask & (1 << (code - ord("A")))
        ]

    def volume_label(self, root: Path) -> str:
        kernel32 = _kernel32()
        if kernel32 is None:
            return ""
        buffer = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(str(root)), buffer, len(buffer), None, None, None, None, 0
        )
        return buffer.value if ok else ""

    def scan(self) -> list[KoboDevice]:
        devices: list[KoboDevice] = []
        for root in self.mounted_roots():
            conf = root / KoboConfRelative
            if not (
                conf.is_file()
                and (root / KoboDbRelative).is_file()
                and (root / KoboVersionRelative).is_file()
            ):
                continue
            firmware, platform, serial = _version_details(root / KoboVersionRelative)
            devices.append(KoboDevice(root, self.volume_label(root), firmware, platform, serial))
        return devices


def eject_volume(root: Path) -> bool:
    """Request a safe media eject. It may fail when Kobo or Explorer still holds it."""
    kernel32 = _kernel32()
    if kernel32 is None or not root.drive:
        return False
    handle = kernel32.CreateFileW(f"\\\\.\\{root.drive}", 0xC0000000, 0x00000003, None, 3, 0, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid_handle):
        return False
    bytes_returned = ctypes.c_ulong()
    try:
        # Lock, dismount, then issue IOCTL_STORAGE_EJECT_MEDIA. Failures are normal
        # if another Windows process still uses the removable volume.
        locked = kernel32.DeviceIoControl(
            handle, 0x00090018, None, 0, None, 0, ctypes.byref(bytes_returned), None
        )
        if not locked:
            return False
        kernel32.DeviceIoControl(
            handle, 0x00090020, None, 0, None, 0, ctypes.byref(bytes_returned), None
        )
        return bool(
            kernel32.DeviceIoControl(
                handle, 0x002D4808, None, 0, None, 0, ctypes.byref(bytes_returned), None
            )
        )
    finally:
        kernel32.CloseHandle(handle)
