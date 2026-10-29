"""BookFlow account login with an in-memory session and no credential storage."""

from __future__ import annotations

import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .pairing import PairingCode, PairingError, parse_pairing_code


class AccountError(ValueError):
    """Safe, user-facing account error without server responses or secrets."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccountError("Het adres verwijst door. Vul het rechtstreekse HTTPS-adres van BookFlow in.")


class BookFlowAccount:
    def __init__(self, origin: str) -> None:
        parsed = urlsplit(origin.strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise AccountError("Vul het HTTPS-adres van BookFlow in, zonder /settings of andere toevoegingen.")
        self.origin = f"https://{parsed.netloc.lower()}"
        self.username: str | None = None
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), _NoRedirect())

    def _request(self, path: str, data: dict | None = None) -> dict:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.origin + path, body, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "X-BookFlow-CSRF": "1",
        })
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(65537)
        except HTTPError as exc:
            messages = {
                401: "Gebruikersnaam of wachtwoord onjuist, of je sessie is verlopen.",
                403: "Inloggen is niet toegestaan. Gebruik bij uitsluitend externe login een koppelcode uit BookFlow.",
                429: "Te veel inlogpogingen. Wacht even en probeer opnieuw.",
            }
            raise AccountError(messages.get(exc.code, "BookFlow kon de aanvraag niet verwerken. Probeer het later opnieuw.")) from None
        except (URLError, OSError):
            raise AccountError("BookFlow is niet bereikbaar. Controleer het adres, je verbinding en het HTTPS-certificaat.") from None
        if len(raw) > 65536:
            raise AccountError("BookFlow gaf een te groot antwoord terug.")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AccountError("Dit adres geeft geen geldig BookFlow-antwoord. Controleer het serveradres.") from None
        if not isinstance(result, dict):
            raise AccountError("BookFlow gaf een ongeldig antwoord terug.")
        return result

    def login(self, username: str, password: str) -> str:
        self.clear()
        if not username.strip() or not password:
            raise AccountError("Vul je gebruikersnaam en wachtwoord in.")
        try:
            result = self._request("/api/auth/login", {
                "username": username.strip(), "password": password, "remember_me": False,
            })
            if result.get("success") is not True:
                raise AccountError("Inloggen is niet gelukt.")
            verified = self._request("/api/auth/check")
            name = verified.get("username")
            if verified.get("authenticated") is not True or not isinstance(name, str) or not name:
                raise AccountError("Log in met een persoonlijk BookFlow-account.")
        except Exception:
            self.clear()
            raise
        self.username = name
        return name

    def pairing_code(self) -> PairingCode:
        if not self.username:
            raise AccountError("Log eerst in bij BookFlow.")
        result = self._request("/api/v1/users/me/kobo-pairing-code", {})
        try:
            code = parse_pairing_code(str(result.get("code", "")))
        except PairingError:
            raise AccountError("BookFlow kon geen geldige koppeling voorbereiden.") from None
        if code.origin != self.origin:
            raise AccountError("De koppeling verwijst naar een andere server. Controleer de BookFlow-configuratie.")
        return code

    def clear(self) -> None:
        self.cookies.clear()
        self.username = None
