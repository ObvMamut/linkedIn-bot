import ipaddress
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from linkedin_bot.domain.models import Contract


class TelegramConfig(Contract):
    transport: Literal["long_polling"] = "long_polling"


class LinkedInConfig(Contract):
    browser_profile_dir: str | None = None
    jobs_interval_minutes: int = Field(default=360, ge=1)
    inbox_interval_minutes: int = Field(default=30, ge=1)
    requests_interval_minutes: int = Field(default=30, ge=1)
    posts_interval_minutes: int = Field(default=360, ge=1)


class InferenceConfig(Contract):
    local_endpoint: str = "http://127.0.0.1:11434"
    max_concurrency: Literal[1] = 1
    task_deadline_seconds: int = Field(default=300, ge=1, le=300)

    @field_validator("local_endpoint")
    @classmethod
    def loopback(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "http" or parts.username or parts.password:
            raise ValueError("Ollama requires an unauthenticated loopback HTTP endpoint")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("Ollama endpoint must be an origin")
        try:
            address = ipaddress.ip_address(parts.hostname or "")
            port = parts.port
        except ValueError as exc:
            raise ValueError("Ollama endpoint must use a literal loopback address") from exc
        if not address.is_loopback or port is None:
            raise ValueError("Ollama endpoint must use loopback and explicit port")
        return value.rstrip("/")


class Config(Contract):
    schema_version: Literal[1] = 1
    timezone: str = "Europe/Warsaw"
    data_dir: str = "~/.local/share/linkedin-bot"
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser().absolute()

    def validate_paths(self) -> None:
        root = self.root
        if root == Path("/") or root == Path.home():
            raise ValueError("data directory must be dedicated to the application")
        for path in (root, *root.parents):
            if path.is_symlink():
                raise ValueError("data path cannot contain symlinks")
        if self.linkedin.browser_profile_dir:
            profile = Path(self.linkedin.browser_profile_dir).expanduser().absolute()
            if not profile.is_relative_to(root / "browser"):
                raise ValueError("LinkedIn profile must be under data_dir/browser")
            for path in (profile, *profile.parents):
                if path.is_symlink():
                    raise ValueError("browser path cannot contain symlinks")


def load_config(path: Path | None) -> Config:
    config = Config() if path is None else Config.model_validate(tomllib.loads(path.read_text()))
    config.validate_paths()
    return config
