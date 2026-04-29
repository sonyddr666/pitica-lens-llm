from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests


CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_ACCOUNTS_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
SESSION_REFRESH_URL = "https://chatgpt.com/api/auth/session"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_MODEL = "gpt-5.4-mini"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

PLACEHOLDER_RE = re.compile(r"(cole_|_aqui|placeholder|example)", re.I)


@dataclass
class CodexAccount:
    id: str
    label: str
    auth_file: Path
    schema: str
    index: int | None
    access_token: str
    refresh_token: str
    account_id: str
    email: str
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and time.time() > self.expires_at - 60)

    @property
    def safe_label(self) -> str:
        return f"{self.label} ({self.auth_file.name})"


def decode_jwt(token: str) -> dict[str, Any]:
    token = clean_bearer(token)
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}


def clean_bearer(token: str) -> str:
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def jwt_email(token: str) -> str:
    claims = decode_jwt(token)
    profile = claims.get("https://api.openai.com/profile", {})
    return profile.get("email") or claims.get("email") or "-"


def jwt_account_id(token: str) -> str:
    claims = decode_jwt(token)
    auth = claims.get("https://api.openai.com/auth", {})
    return auth.get("chatgpt_account_id") or ""


def jwt_exp(token: str) -> float:
    claims = decode_jwt(token)
    try:
        return float(claims.get("exp") or 0)
    except Exception:
        return 0.0


def to_epoch_s(raw: Any) -> float:
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except Exception:
        return 0.0
    return value / 1000 if value > 1e10 else value


def parse_quota(body: Any) -> dict[str, Any]:
    quota: dict[str, Any] = {"plan": "-"}
    if isinstance(body, dict):
        quota["plan"] = body.get("plan_type") or body.get("plan") or "-"
    found: list[tuple[float, float | None]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            pct = obj.get("percent_left")
            if pct is None:
                pct = obj.get("remaining_percent")
            if pct is None and obj.get("used_percent") is not None:
                pct = 100.0 - float(obj["used_percent"])
            if pct is not None:
                reset = obj.get("reset_time_ms") or obj.get("reset_at")
                if not reset and obj.get("reset_after_seconds") is not None:
                    reset = time.time() + float(obj["reset_after_seconds"])
                if not reset and isinstance(obj.get("primary_window"), dict):
                    reset = obj["primary_window"].get("reset_time_ms")
                found.append((float(pct), to_epoch_s(reset) if reset else None))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(body)
    if found:
        quota["five_hour_pct"] = found[0][0]
        quota["five_hour_reset"] = found[0][1]
    if len(found) > 1:
        quota["weekly_pct"] = found[1][0]
        quota["weekly_reset"] = found[1][1]
    return quota


class CodexService:
    def __init__(
        self,
        root: str | Path = ".",
        model: str = DEFAULT_MODEL,
        selected_account_id: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.root = Path(root)
        self.model = model or DEFAULT_MODEL
        self.selected_account_id = selected_account_id
        self.timeout = timeout
        self._lock = threading.Lock()
        self._accounts_cache: list[CodexAccount] = []

    def list_accounts(self, refresh_cache: bool = True) -> list[CodexAccount]:
        if refresh_cache or not self._accounts_cache:
            self._accounts_cache = self._load_accounts()
        return list(self._accounts_cache)

    def select_account(self, account_id: str | None) -> None:
        self.selected_account_id = account_id or None

    def _load_accounts(self) -> list[CodexAccount]:
        accounts: list[CodexAccount] = []
        for auth_file in sorted(self.root.glob("auth*.json")):
            try:
                data = json.loads(auth_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            pool = None
            if isinstance(data, dict):
                pool = data.get("credential_pool", {}).get("openai-codex")
            if isinstance(pool, list):
                for index, entry in enumerate(pool):
                    if isinstance(entry, dict):
                        account = self._account_from_pool_entry(auth_file, entry, index)
                        if account:
                            accounts.append(account)
                continue

            account = self._account_from_flat(auth_file, data)
            if account:
                accounts.append(account)

        return accounts

    def _account_from_pool_entry(self, auth_file: Path, entry: dict[str, Any], index: int) -> CodexAccount | None:
        access = clean_bearer(str(entry.get("access_token") or ""))
        refresh = str(entry.get("refresh_token") or "")
        if not access or PLACEHOLDER_RE.search(access):
            return None
        account_id = str(entry.get("account_id") or entry.get("accountId") or jwt_account_id(access))
        label = str(entry.get("label") or entry.get("id") or jwt_email(access) or f"conta_{index + 1}")
        return CodexAccount(
            id=f"{auth_file.name}:pool:{index}",
            label=label,
            auth_file=auth_file,
            schema="pool",
            index=index,
            access_token=access,
            refresh_token=refresh,
            account_id=account_id,
            email=jwt_email(access),
            expires_at=jwt_exp(access),
        )

    def _account_from_flat(self, auth_file: Path, data: Any) -> CodexAccount | None:
        if not isinstance(data, dict):
            return None
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        access = clean_bearer(str(tokens.get("access_token") or tokens.get("access") or data.get("access") or ""))
        refresh = str(tokens.get("refresh_token") or tokens.get("refresh") or data.get("refresh") or "")
        if not access or PLACEHOLDER_RE.search(access):
            return None
        account_id = str(tokens.get("account_id") or tokens.get("accountId") or data.get("accountId") or data.get("account_id") or jwt_account_id(access))
        expires_at = to_epoch_s(tokens.get("expires_at") or data.get("expires") or jwt_exp(access))
        return CodexAccount(
            id=f"{auth_file.name}:flat:0",
            label=auth_file.stem,
            auth_file=auth_file,
            schema="tokens" if tokens else "flat",
            index=None,
            access_token=access,
            refresh_token=refresh,
            account_id=account_id,
            email=jwt_email(access),
            expires_at=expires_at or jwt_exp(access),
        )

    def get_active_account(self) -> CodexAccount:
        with self._lock:
            accounts = self.list_accounts(refresh_cache=True)
            if not accounts:
                raise RuntimeError("Nenhuma conta Codex valida encontrada em auth*.json.")

            selected = next((a for a in accounts if a.id == self.selected_account_id), None)
            account = selected or self._best_account(accounts)

            if account.is_expired:
                self.refresh_account(account)
                accounts = self.list_accounts(refresh_cache=True)
                account = next((a for a in accounts if a.id == account.id), account)

            return account

    @staticmethod
    def _best_account(accounts: list[CodexAccount]) -> CodexAccount:
        not_expired = [a for a in accounts if not a.is_expired]
        return (not_expired or accounts)[0]

    def headers_for(self, account: CodexAccount, stream: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {account.access_token}",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": USER_AGENT,
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        if account.account_id:
            headers["ChatGPT-Account-Id"] = account.account_id
            headers["chatgpt-account-id"] = account.account_id
        return headers

    def verify_account(self, account: CodexAccount) -> dict[str, Any]:
        if account.is_expired and account.refresh_token:
            try:
                self.refresh_account(account)
                account = next((a for a in self.list_accounts(True) if a.id == account.id), account)
            except Exception:
                pass

        try:
            response = requests.get(CODEX_USAGE_URL, headers=self.headers_for(account), timeout=20)
        except Exception as exc:
            return self._status_for(account, "ERRO CONEXAO", error=str(exc))

        status = self._status_for(account, f"HTTP {response.status_code}", http_status=response.status_code)
        if response.status_code == 200:
            status["result"] = "OK"
            try:
                status["quota"] = parse_quota(response.json())
            except Exception:
                status["quota"] = {}
            status["workspace"] = self.get_workspace_name(account)
        elif response.status_code == 401:
            status["result"] = "EXPIRADO"
        elif response.status_code == 429:
            status["result"] = "ESGOTADO"
        return status

    def _status_for(self, account: CodexAccount, result: str, **extra: Any) -> dict[str, Any]:
        status = {
            "id": account.id,
            "label": account.label,
            "file": account.auth_file.name,
            "email": account.email,
            "expires_at": account.expires_at,
            "result": result,
            "quota": {},
            "workspace": "-",
            "http_status": "-",
        }
        status.update(extra)
        return status

    def get_workspace_name(self, account: CodexAccount) -> str:
        try:
            response = requests.get(CODEX_ACCOUNTS_URL, headers=self.headers_for(account), timeout=8)
            if response.status_code != 200:
                return "-"
            accounts = response.json().get("accounts", {})
            if account.account_id and account.account_id in accounts:
                return accounts[account.account_id].get("name") or "Personal"
            for value in accounts.values():
                if isinstance(value, dict) and value.get("is_active"):
                    return value.get("name") or "Personal"
        except Exception:
            return "-"
        return "-"

    def refresh_account(self, account: CodexAccount) -> None:
        if not account.refresh_token:
            raise RuntimeError(f"Conta sem refresh token: {account.safe_label}")

        payload = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "refresh_token": account.refresh_token,
        }
        response = requests.post(
            OAUTH_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
            timeout=30,
        )

        if response.status_code != 200:
            session_response = requests.post(
                SESSION_REFRESH_URL,
                headers={"Content-Type": "application/json", "Origin": "https://chatgpt.com", "Referer": "https://chatgpt.com/", "User-Agent": USER_AGENT},
                json={"refreshToken": account.refresh_token},
                timeout=30,
            )
            if session_response.status_code != 200:
                raise RuntimeError(f"Falha ao renovar {account.safe_label}: HTTP {response.status_code}")
            data = session_response.json()
            new_auth = {
                "access_token": data.get("accessToken") or data.get("access_token"),
                "refresh_token": data.get("refreshToken") or data.get("refresh_token") or account.refresh_token,
                "expires_in": 3600,
            }
        else:
            new_auth = response.json()

        new_access = clean_bearer(str(new_auth.get("access_token") or ""))
        if not new_access:
            raise RuntimeError(f"Renovacao sem access token: {account.safe_label}")

        new_refresh = str(new_auth.get("refresh_token") or account.refresh_token)
        expires_at_ms = int((time.time() + int(new_auth.get("expires_in") or 3600)) * 1000)
        self._update_auth_file(account, new_access, new_refresh, expires_at_ms)

    def _update_auth_file(self, account: CodexAccount, access: str, refresh: str, expires_at_ms: int) -> None:
        data = json.loads(account.auth_file.read_text(encoding="utf-8"))
        if account.schema == "pool":
            pool = data.get("credential_pool", {}).get("openai-codex", [])
            if account.index is None or account.index >= len(pool):
                raise RuntimeError("Indice de conta invalido para atualizar auth.")
            pool[account.index]["access_token"] = access
            pool[account.index]["refresh_token"] = refresh
        elif account.schema == "tokens":
            data.setdefault("tokens", {})
            data["tokens"]["access_token"] = access
            data["tokens"]["refresh_token"] = refresh
            data["tokens"]["expires_at"] = expires_at_ms
        else:
            data["access"] = access
            data["refresh"] = refresh
            data["expires"] = expires_at_ms
        account.auth_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        print_paths: list[str | Path] | None = None,
        image_paths: list[str | Path] | None = None,
        reasoning_effort: str = "medium",
    ) -> str:
        account = self.get_active_account()
        request_messages = list(messages)
        if print_paths:
            joined = "\n".join(f"- {Path(path)}" for path in print_paths)
            request_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Arquivos de print salvos localmente para referencia da interface.\n"
                        "A analise visual vem do Google Lens no prompt anterior.\n"
                        f"{joined}"
                    ),
                }
            )

        payload = {
            "model": model or self.model or DEFAULT_MODEL,
            "instructions": self._extract_system(request_messages),
            "input": self._responses_input(request_messages, image_paths=image_paths),
            "stream": True,
            "store": False,
        }
        effort = (reasoning_effort or "medium").strip().lower()
        if effort not in {"none", "off", "desligado"}:
            if effort not in {"low", "medium", "high", "xhigh"}:
                effort = "medium"
            payload["reasoning"] = {"effort": effort, "summary": "auto"}

        response = requests.post(
            CODEX_RESPONSES_URL,
            headers=self.headers_for(account, stream=True),
            json=payload,
            stream=True,
            timeout=self.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"Codex HTTP {response.status_code}: {response.text[:300]}")

        text_parts: list[str] = []
        fallback_parts: list[str] = []
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else str(raw_line)
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except Exception:
                continue
            delta = self._event_delta(event)
            if delta:
                text_parts.append(delta)
                if on_delta:
                    on_delta(delta)
                continue
            completed = self._completed_text(event)
            if completed:
                fallback_parts.append(completed)

        final = "".join(text_parts).strip()
        if not final and fallback_parts:
            final = "".join(fallback_parts).strip()
        if not final:
            raise RuntimeError("Codex respondeu sem texto.")
        return final

    @staticmethod
    def _extract_system(messages: list[dict[str, str]]) -> str:
        systems = [m.get("content", "") for m in messages if m.get("role") == "system" and m.get("content")]
        return "\n\n".join(systems).strip() or (
            "Voce e uma assistente em pt-BR integrada a print, STT e Google Lens. "
            "Responda de forma util, direta e completa."
        )

    @staticmethod
    @staticmethod
    def _image_content_parts(image_paths: list[str | Path] | None) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for raw_path in image_paths or []:
            path = Path(raw_path)
            if not path.exists() or not path.is_file():
                continue
            mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
            data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
            parts.append({"type": "input_image", "image_url": data_url, "detail": "auto"})
        return parts

    @classmethod
    def _responses_input(
        cls,
        messages: list[dict[str, str]],
        image_paths: list[str | Path] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        last_user_item: dict[str, Any] | None = None
        for message in messages:
            role = message.get("role", "user")
            if role == "system":
                continue
            api_role = "assistant" if role == "assistant" else "user"
            content_type = "output_text" if api_role == "assistant" else "input_text"
            text = str(message.get("content") or "").strip()
            if not text:
                continue
            item = {
                "type": "message",
                "role": api_role,
                "content": [{"type": content_type, "text": text}],
            }
            items.append(item)
            if api_role == "user":
                last_user_item = item
        if not items:
            last_user_item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Oi."}]}
            items.append(last_user_item)
        image_parts = cls._image_content_parts(image_paths)
        if image_parts:
            if last_user_item is None:
                last_user_item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Imagem anexada."}]}
                items.append(last_user_item)
            last_user_item["content"].extend(image_parts)
        return items

    @staticmethod
    def _event_delta(event: dict[str, Any]) -> str:
        event_type = event.get("type")
        if event_type in {"response.output_text.delta", "response.text.delta"}:
            return str(event.get("delta") or "")
        if event_type in {"response.refusal.delta"}:
            return str(event.get("delta") or "")
        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            return str(choices[0].get("delta", {}).get("content") or "")
        return ""

    @staticmethod
    def _completed_text(event: dict[str, Any]) -> str:
        texts: list[str] = []
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        for item in response.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("text"):
                    texts.append(str(content["text"]))
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        for content in item.get("content", []) or []:
            if content.get("text"):
                texts.append(str(content["text"]))
        return "".join(texts)
