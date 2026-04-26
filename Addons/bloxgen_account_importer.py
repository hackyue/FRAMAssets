"""FRAM addon for generating BloxGen accounts and adding them to FRAM."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
import json
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib import error, parse, request


ADDON_NAME = "BloxGen Account Importer"
ADDON_DESCRIPTION = "Generate BloxGen accounts and import them directly into FRAM."
ADDON_FRAM_VERSION = "2.5.1"

API_KEY_SETTING = "bloxgen_api_key"
ACCOUNT_TYPE_SETTING = "bloxgen_account_type"


class BloxGenError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        time_remaining: Optional[int] = None,
    ) -> None:
        normalized_message = str(message or "BloxGen request failed.").strip() or "BloxGen request failed."
        if time_remaining is not None and time_remaining > 0:
            normalized_message = f"{normalized_message} Try again in {time_remaining}s."
        super().__init__(normalized_message)
        self.status_code = status_code
        self.time_remaining = time_remaining


@dataclass(frozen=True)
class BloxGenAccountType:
    name: str
    price: float
    in_stock: bool

    @property
    def display_label(self) -> str:
        availability = "In stock" if self.in_stock else "Out of stock"
        return f"{self.name} | {format_currency(self.price)} | {availability}"


@dataclass(frozen=True)
class BloxGenDailyLimit:
    generations_today: int
    remaining_generations: int
    daily_limit: int
    is_resell_role: bool
    reset_time: str


@dataclass(frozen=True)
class BloxGenOverview:
    balance: float
    account_types: tuple[BloxGenAccountType, ...]
    daily_limit: BloxGenDailyLimit


@dataclass(frozen=True)
class BloxGenGeneratedAccount:
    username: str
    password: str
    cookie: str
    account_type: str
    cost: float
    account_id: int
    avatar_url: str
    full_avatar_url: str
    robux: int
    rap: int
    summary: str
    region: str


@dataclass(frozen=True)
class BloxGenAddResult:
    generated_account: BloxGenGeneratedAccount
    saved_username: str
    saved_via_fallback: bool
    overview: Optional[BloxGenOverview]


def format_currency(value: float) -> str:
    formatted = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return f"${formatted or '0'}"


def format_reset_time(value: str) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %I:%M %p")


class BloxGenClient:
    def __init__(self, base_url: str = "https://core.bloxgen.net/api", timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_balance(self, api_key: str) -> float:
        data = self._request_data("GET", "/balance", query={"apiKey": api_key})
        return self._coerce_float(data.get("balance"), "BloxGen did not return a valid balance.")

    def get_prices(self, api_key: str) -> dict[str, float]:
        data = self._request_data("GET", "/prices", query={"apiKey": api_key})
        prices: dict[str, float] = {}
        for key, value in data.items():
            prices[str(key)] = self._coerce_float(value, f"Invalid price for account type '{key}'.")
        return prices

    def get_stock(self, api_key: str) -> dict[str, bool]:
        data = self._request_data("GET", "/stock", query={"apiKey": api_key})
        stock: dict[str, bool] = {}
        for key, value in data.items():
            stock[str(key)] = self._coerce_bool(value, f"Invalid stock value for account type '{key}'.")
        return stock

    def get_daily_limit(self, api_key: str) -> BloxGenDailyLimit:
        data = self._request_data("GET", "/daily-limit", query={"apiKey": api_key})
        return BloxGenDailyLimit(
            generations_today=self._coerce_int(data.get("generationsToday"), "BloxGen did not return generationsToday."),
            remaining_generations=self._coerce_int(data.get("remainingGenerations"), "BloxGen did not return remainingGenerations."),
            daily_limit=self._coerce_int(data.get("dailyLimit"), "BloxGen did not return dailyLimit."),
            is_resell_role=self._coerce_bool(data.get("isResellRole"), "BloxGen did not return isResellRole."),
            reset_time=self._coerce_optional_string(data.get("resetTime")),
        )

    def generate(self, api_key: str, account_type: str) -> BloxGenGeneratedAccount:
        data = self._request_data(
            "POST",
            "/generate",
            payload={"apiKey": api_key, "type": account_type},
        )
        return BloxGenGeneratedAccount(
            username=self._coerce_string(data.get("username"), "BloxGen did not return a username."),
            password=self._coerce_string(data.get("password"), "BloxGen did not return a password."),
            cookie=self._coerce_string(data.get("cookie"), "BloxGen did not return a cookie."),
            account_type=self._coerce_string(data.get("type"), "BloxGen did not return an account type."),
            cost=self._coerce_float(data.get("cost"), "BloxGen did not return a valid cost."),
            account_id=self._coerce_int(data.get("id"), "BloxGen did not return an account id."),
            avatar_url=self._coerce_optional_string(data.get("avatarUrl")),
            full_avatar_url=self._coerce_optional_string(data.get("fullAvatarUrl")),
            robux=self._coerce_optional_int(data.get("robux")),
            rap=self._coerce_optional_int(data.get("rap")),
            summary=self._coerce_optional_string(data.get("summary")),
            region=self._coerce_optional_string(data.get("region")),
        )

    def fetch_overview(self, api_key: str) -> BloxGenOverview:
        balance = self.get_balance(api_key)
        daily_limit = self.get_daily_limit(api_key)
        prices = self.get_prices(api_key)
        stock = self.get_stock(api_key)

        ordered_names = list(prices.keys())
        for name in stock:
            if name not in prices:
                ordered_names.append(name)

        account_types = tuple(
            BloxGenAccountType(
                name=name,
                price=float(prices.get(name, 0.0)),
                in_stock=bool(stock.get(name, False)),
            )
            for name in ordered_names
        )

        if not account_types:
            raise BloxGenError("BloxGen did not return any account types.")

        return BloxGenOverview(
            balance=balance,
            account_types=account_types,
            daily_limit=daily_limit,
        )

    def _request_data(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "FRAM-BloxGen-Addon/1.0",
        }
        body: Optional[bytes] = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request_object = request.Request(url=url, data=body, headers=headers, method=method)

        try:
            with request.urlopen(request_object, timeout=self.timeout_seconds) as response:
                raw_body = response.read()
        except error.HTTPError as exc:
            raw_body = exc.read()
            raise self._parse_http_error(raw_body, exc.code) from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise BloxGenError(f"Unable to reach BloxGen: {reason}.") from exc

        payload_data = self._decode_json(raw_body)
        return self._extract_data(payload_data)

    def _parse_http_error(self, raw_body: bytes, status_code: int) -> BloxGenError:
        try:
            payload_data = self._decode_json(raw_body)
        except BloxGenError:
            return BloxGenError(f"BloxGen request failed with HTTP {status_code}.", status_code=status_code)

        message = self._coerce_optional_string(payload_data.get("message")).strip() or f"BloxGen request failed with HTTP {status_code}."
        time_remaining = self._coerce_optional_int(payload_data.get("timeRemaining"), default=None)
        return BloxGenError(message, status_code=status_code, time_remaining=time_remaining)

    def _decode_json(self, raw_body: bytes) -> Mapping[str, Any]:
        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise BloxGenError("BloxGen returned a non-UTF8 response.") from exc
        except json.JSONDecodeError as exc:
            raise BloxGenError("BloxGen returned invalid JSON.") from exc

        if not isinstance(decoded, Mapping):
            raise BloxGenError("BloxGen returned an unexpected response shape.")
        return decoded

    def _extract_data(self, payload_data: Mapping[str, Any]) -> Mapping[str, Any]:
        success = payload_data.get("success")
        if success is not True:
            message = self._coerce_optional_string(payload_data.get("message")).strip() or "BloxGen request failed."
            time_remaining = self._coerce_optional_int(payload_data.get("timeRemaining"), default=None)
            raise BloxGenError(message, time_remaining=time_remaining)

        data = payload_data.get("data")
        if not isinstance(data, Mapping):
            raise BloxGenError("BloxGen returned a successful response without data.")
        return data

    def _coerce_string(self, value: Any, error_message: str) -> str:
        text = self._coerce_optional_string(value).strip()
        if not text:
            raise BloxGenError(error_message)
        return text

    def _coerce_optional_string(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    def _coerce_int(self, value: Any, error_message: str) -> int:
        if isinstance(value, bool):
            raise BloxGenError(error_message)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                try:
                    return int(stripped)
                except ValueError as exc:
                    raise BloxGenError(error_message) from exc
        raise BloxGenError(error_message)

    def _coerce_optional_int(self, value: Any, default: Optional[int] = 0) -> Optional[int]:
        if value is None:
            return default
        try:
            return self._coerce_int(value, "BloxGen returned an invalid integer.")
        except BloxGenError:
            return default

    def _coerce_float(self, value: Any, error_message: str) -> float:
        if isinstance(value, bool):
            raise BloxGenError(error_message)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                try:
                    return float(stripped)
                except ValueError as exc:
                    raise BloxGenError(error_message) from exc
        raise BloxGenError(error_message)

    def _coerce_bool(self, value: Any, error_message: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        raise BloxGenError(error_message)


class BloxGenAccountImporterAddon:
    def __init__(self, parent: Any, api: Any) -> None:
        self.parent = parent
        self.api = api
        self.ui = api.ui
        self.client = BloxGenClient()
        self._busy = False
        self._overview: Optional[BloxGenOverview] = None
        self._account_types_by_label: dict[str, BloxGenAccountType] = {}

        self.api_key_var = tk.StringVar(value=str(self.api.get_setting(API_KEY_SETTING, "") or ""))
        self.account_type_var = tk.StringVar()
        self.show_api_key_var = tk.BooleanVar(value=False)
        self.balance_var = tk.StringVar(value="Not loaded")
        self.limit_var = tk.StringVar(value="Not loaded")
        self.selected_type_var = tk.StringVar(value="Not selected")
        self.selected_price_var = tk.StringVar(value="Not loaded")
        self.stock_var = tk.StringVar(value="Not loaded")
        self.reset_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="Enter a BloxGen API key, refresh the catalog, then generate an account.")
        self.last_account_var = tk.StringVar(value="Last added: none in this session.")

        self._build()
        self._toggle_api_key_visibility()

    def _build(self) -> None:
        try:
            self.parent.configure(bg=self.ui.BG_DARK)
        except tk.TclError:
            pass

        self.container = tk.Frame(self.parent, bg=self.ui.BG_DARK, highlightthickness=0, bd=0)
        self.container.pack(fill="both", expand=True)

        controls_card = tk.Frame(
            self.container,
            bg=self.ui.BG_LIGHT,
            highlightbackground=self.ui.BORDER_COLOR,
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=14,
        )
        controls_card.pack(fill="x", pady=(0, 12))

        key_row = tk.Frame(controls_card, bg=self.ui.BG_LIGHT, highlightthickness=0, bd=0)
        key_row.pack(fill="x")
        tk.Label(
            key_row,
            text="API Key",
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(side="left")

        self.api_key_entry = ttk.Entry(
            key_row,
            textvariable=self.api_key_var,
            width=44,
            style="Dark.TEntry",
        )
        self.api_key_entry.pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.api_key_entry.bind("<FocusOut>", self._on_api_key_focus_out)
        self.api_key_entry.bind("<Return>", self._on_refresh_requested)

        self.refresh_button = ttk.Button(
            key_row,
            text="Refresh Data",
            style="Dark.TButton",
            command=self.refresh_overview,
        )
        self.refresh_button.pack(side="left")

        self.generate_button = ttk.Button(
            key_row,
            text="Generate + Add",
            style="Dark.TButton",
            command=self.generate_and_add_account,
        )
        self.generate_button.pack(side="left", padx=(8, 0))

        detail_row = tk.Frame(controls_card, bg=self.ui.BG_LIGHT, highlightthickness=0, bd=0)
        detail_row.pack(fill="x", pady=(10, 0))

        ttk.Checkbutton(
            detail_row,
            text="Show API Key",
            variable=self.show_api_key_var,
            style="Dark.TCheckbutton",
            command=self._toggle_api_key_visibility,
        ).pack(side="left")

        tk.Label(
            detail_row,
            text="Account Type",
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(side="left", padx=(18, 8))

        self.account_type_combo = ttk.Combobox(
            detail_row,
            textvariable=self.account_type_var,
            state="readonly",
            style="Dark.TCombobox",
            width=38,
        )
        self.account_type_combo.pack(side="left", fill="x", expand=True)
        self.account_type_combo.bind("<<ComboboxSelected>>", self._on_account_type_selected)

        tk.Label(
            controls_card,
            text="The API key is stored in FRAM settings. Refreshing loads the latest BloxGen catalog before generation.",
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(10, 0))
        tk.Label(
            controls_card,
            textvariable=self.last_account_var,
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_ACCENT_ALT,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        summary = tk.Frame(self.container, bg=self.ui.BG_DARK, highlightthickness=0, bd=0)
        summary.pack(fill="x", pady=(0, 12))
        summary.grid_columnconfigure(0, weight=1)
        summary.grid_columnconfigure(1, weight=1)
        summary.grid_columnconfigure(2, weight=1)
        summary.grid_columnconfigure(3, weight=1)

        self._build_summary_card(summary, 0, "Balance", self.balance_var, self.ui.FG_ACCENT)
        self._build_summary_card(summary, 1, "Daily Limit", self.limit_var, "#5fbf88")
        self._build_summary_card(summary, 2, "Selected Type", self.selected_type_var, self.ui.FG_ACCENT_ALT)
        self._build_summary_card(summary, 3, "Selected Price", self.selected_price_var, "#d7a968")

        status_card = tk.Frame(
            self.container,
            bg=self.ui.BG_LIGHT,
            highlightbackground=self.ui.BORDER_COLOR,
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=14,
        )
        status_card.pack(fill="both", expand=True)

        header_row = tk.Frame(status_card, bg=self.ui.BG_LIGHT, highlightthickness=0, bd=0)
        header_row.pack(fill="x")

        tk.Label(
            header_row,
            text="Status",
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(side="left")

        self.stock_label = tk.Label(
            header_row,
            textvariable=self.stock_var,
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_ACCENT_ALT,
            font=("Segoe UI", 9, "bold"),
            anchor="e",
        )
        self.stock_label.pack(side="right")

        tk.Label(
            status_card,
            textvariable=self.status_var,
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=780,
        ).pack(anchor="w", fill="x", pady=(8, 8))

        tk.Label(
            status_card,
            textvariable=self.reset_var,
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
        ).pack(anchor="w")

    def _build_summary_card(
        self,
        parent: Any,
        column: int,
        title: str,
        value_variable: tk.StringVar,
        accent_color: str,
    ) -> None:
        card = tk.Frame(
            parent,
            bg=self.ui.BG_LIGHT,
            highlightbackground=self.ui.BORDER_COLOR,
            highlightthickness=1,
            bd=0,
            padx=12,
            pady=12,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column < 3 else (0, 0))

        tk.Label(
            card,
            text=title,
            bg=self.ui.BG_LIGHT,
            fg=self.ui.FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(anchor="w")

        tk.Label(
            card,
            textvariable=value_variable,
            bg=self.ui.BG_LIGHT,
            fg=accent_color,
            font=("Segoe UI", 11, "bold"),
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def _on_api_key_focus_out(self, _event: Any) -> None:
        self._persist_api_key()

    def _on_refresh_requested(self, _event: Any) -> None:
        self.refresh_overview()

    def _toggle_api_key_visibility(self) -> None:
        show_value = "" if self.show_api_key_var.get() else "*"
        self.api_key_entry.configure(show=show_value)

    def _persist_api_key(self) -> str:
        api_key = self.api_key_var.get().strip()
        self.api.set_setting(API_KEY_SETTING, api_key)
        return api_key

    def refresh_overview(self) -> None:
        api_key = self._persist_api_key()
        if not api_key:
            self.status_var.set("Enter a BloxGen API key before refreshing.")
            self.api.show_error("Enter a BloxGen API key before refreshing.")
            return

        self._run_background_task(
            busy_message="Loading BloxGen balance, stock, prices, and daily limits...",
            worker=lambda: self._refresh_overview_worker(api_key),
        )

    def _refresh_overview_worker(self, api_key: str) -> None:
        overview = self.client.fetch_overview(api_key)
        self.api.run_on_ui_thread(self._apply_overview, overview)

    def _apply_overview(self, overview: BloxGenOverview) -> None:
        self._overview = overview
        self.balance_var.set(format_currency(overview.balance))
        self.limit_var.set(f"{overview.daily_limit.remaining_generations} left of {overview.daily_limit.daily_limit}")
        self.reset_var.set(
            f"Reset: {format_reset_time(overview.daily_limit.reset_time)} | Generated today: {overview.daily_limit.generations_today}"
        )

        labels = [account_type.display_label for account_type in overview.account_types]
        self._account_types_by_label = {
            account_type.display_label: account_type
            for account_type in overview.account_types
        }
        self.account_type_combo.configure(values=labels)

        saved_account_type = str(self.api.get_setting(ACCOUNT_TYPE_SETTING, "") or "").strip()
        selected_label = ""
        for account_type in overview.account_types:
            if account_type.name == saved_account_type:
                selected_label = account_type.display_label
                break
        if not selected_label:
            for account_type in overview.account_types:
                if account_type.in_stock:
                    selected_label = account_type.display_label
                    break
        if not selected_label and labels:
            selected_label = labels[0]

        self.account_type_var.set(selected_label)
        self._update_selected_account_type_details()
        self.status_var.set("BloxGen catalog loaded. Generate an account to add it to FRAM.")
        self._set_busy(False)

    def generate_and_add_account(self) -> None:
        api_key = self._persist_api_key()
        if not api_key:
            self.status_var.set("Enter a BloxGen API key before generating an account.")
            self.api.show_error("Enter a BloxGen API key before generating an account.")
            return

        selected_account_type = self._get_selected_account_type()
        if selected_account_type is None:
            self.status_var.set("Refresh BloxGen data and choose an account type before generating.")
            self.api.show_error("Refresh BloxGen data and choose an account type before generating.")
            return
        if not selected_account_type.in_stock:
            self.status_var.set(f"{selected_account_type.name} is currently out of stock.")
            self.api.show_error(f"{selected_account_type.name} is currently out of stock.")
            return

        self._run_background_task(
            busy_message=f"Generating a {selected_account_type.name} account and adding it to FRAM...",
            worker=lambda: self._generate_and_add_worker(api_key, selected_account_type),
        )

    def _generate_and_add_worker(self, api_key: str, selected_account_type: BloxGenAccountType) -> None:
        generated_account = self.client.generate(api_key, selected_account_type.name)
        saved_username, saved_via_fallback = self._save_generated_account(generated_account)

        overview: Optional[BloxGenOverview] = None
        try:
            overview = self.client.fetch_overview(api_key)
        except (BloxGenError, ValueError, TypeError):
            overview = None

        result = BloxGenAddResult(
            generated_account=generated_account,
            saved_username=saved_username,
            saved_via_fallback=saved_via_fallback,
            overview=overview,
        )
        self.api.run_on_ui_thread(self._apply_generation_result, result)

    def _save_generated_account(self, generated_account: BloxGenGeneratedAccount) -> tuple[str, bool]:
        success, imported_username = self.api.manager.import_cookie_account(generated_account.cookie)
        if success and imported_username:
            existing_record = self.api.manager.accounts.get(imported_username)
            self.api.manager.accounts[imported_username] = self._build_account_record(
                username=imported_username,
                generated_account=generated_account,
                existing_record=existing_record,
            )
            self.api.manager.save_accounts()
            return imported_username, False

        existing_record = self.api.manager.accounts.get(generated_account.username)
        self.api.manager.accounts[generated_account.username] = self._build_account_record(
            username=generated_account.username,
            generated_account=generated_account,
            existing_record=existing_record,
        )
        self.api.manager.save_accounts()
        return generated_account.username, True

    def _build_account_record(
        self,
        *,
        username: str,
        generated_account: BloxGenGeneratedAccount,
        existing_record: Any,
    ) -> dict[str, Any]:
        note = ""
        group = ""
        vip_server = ""
        auto_rejoin_enabled = False
        added_date = time.strftime("%Y-%m-%d %H:%M:%S")
        existing_user_id = ""

        if isinstance(existing_record, Mapping):
            note = str(existing_record.get("note") or "")
            group = str(existing_record.get("group") or "")
            vip_server = str(existing_record.get("vip_server") or "")
            auto_rejoin_enabled = bool(existing_record.get("auto_rejoin_enabled", False))
            added_date = str(existing_record.get("added_date") or added_date)
            existing_user_id = str(existing_record.get("user_id") or "")

        user_id = existing_user_id
        if generated_account.account_id > 0:
            user_id = str(generated_account.account_id)

        return {
            "username": username,
            "cookie": generated_account.cookie,
            "password": generated_account.password,
            "added_date": added_date,
            "note": note,
            "group": group,
            "vip_server": vip_server,
            "auto_rejoin_enabled": auto_rejoin_enabled,
            "user_id": user_id,
        }

    def _apply_generation_result(self, result: BloxGenAddResult) -> None:
        if result.overview is not None:
            self._overview = result.overview
            self._apply_overview(result.overview)

        generated_account = result.generated_account
        region = generated_account.region.strip() or "Unknown region"
        self.last_account_var.set(
            f"Last added: {result.saved_username} | {generated_account.account_type} | {region} | {format_currency(generated_account.cost)}"
        )

        if result.saved_via_fallback:
            status_message = (
                f"Added {result.saved_username} to FRAM using the BloxGen response because FRAM could not validate the cookie automatically."
            )
        else:
            status_message = f"Added {result.saved_username} to FRAM and refreshed the account list."

        self.status_var.set(status_message)
        self.api.refresh_accounts(selected_usernames=[result.saved_username])
        self._set_busy(False)
        self.api.show_success(status_message)

    def _run_background_task(self, busy_message: str, worker: Callable[[], None]) -> None:
        if self._busy:
            return
        self._set_busy(True, busy_message)
        thread = threading.Thread(
            target=self._background_task_runner,
            args=(worker,),
            daemon=True,
            name="BloxGenAccountImporter",
        )
        thread.start()

    def _background_task_runner(self, worker: Callable[[], None]) -> None:
        try:
            worker()
        except (BloxGenError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.api.run_on_ui_thread(self._handle_task_failure, str(exc))

    def _handle_task_failure(self, message: str) -> None:
        self.status_var.set(message)
        self._set_busy(False)
        self.api.show_error(message)

    def _get_selected_account_type(self) -> Optional[BloxGenAccountType]:
        selected_label = self.account_type_var.get().strip()
        if not selected_label:
            return None
        return self._account_types_by_label.get(selected_label)

    def _on_account_type_selected(self, _event: Any) -> None:
        self._update_selected_account_type_details()

    def _update_selected_account_type_details(self) -> None:
        selected_account_type = self._get_selected_account_type()
        if selected_account_type is None:
            self.selected_type_var.set("Not selected")
            self.selected_price_var.set("Not loaded")
            self.stock_var.set("Stock: not loaded")
            return

        self.selected_type_var.set(selected_account_type.name)
        self.selected_price_var.set(format_currency(selected_account_type.price))
        stock_label = "In stock" if selected_account_type.in_stock else "Out of stock"
        self.stock_var.set(f"Stock: {stock_label}")
        self.api.set_setting(ACCOUNT_TYPE_SETTING, selected_account_type.name)

    def _set_busy(self, busy: bool, status_message: Optional[str] = None) -> None:
        self._busy = busy
        if status_message is not None:
            self.status_var.set(status_message)
        button_state = "disabled" if busy else "normal"
        combo_state = "disabled" if busy else "readonly"
        self.refresh_button.configure(state=button_state)
        self.generate_button.configure(state=button_state)
        self.account_type_combo.configure(state=combo_state)


def build_tab(parent: Any, api: Any) -> BloxGenAccountImporterAddon:
    return BloxGenAccountImporterAddon(parent, api)
