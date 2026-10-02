"""Settings read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")).resolve())

    clipper_token: str = field(default_factory=lambda: os.getenv("DISCORD_CLIPPER_TOKEN", ""))
    rater_token: str = field(default_factory=lambda: os.getenv("DISCORD_RATER_TOKEN", ""))
    # When set, slash commands are synced to this guild instantly instead of globally (~1h).
    guild_id: int = field(default_factory=lambda: _int("DISCORD_GUILD_ID", 0))

    # Which AI does the thinking: "ollama" (free, runs on this server), "anthropic" (Claude, paid) or "none".
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama").strip().lower())
    # Optional override just for rating clips / learning from viral clips (e.g. "anthropic").
    rater_llm_provider: str = field(default_factory=lambda: os.getenv("RATER_LLM_PROVIDER", "").strip().lower())

    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/"))
    ollama_text_model: str = field(default_factory=lambda: os.getenv("OLLAMA_TEXT_MODEL", "qwen2.5:7b"))
    ollama_vision_model: str = field(default_factory=lambda: os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:7b"))
    ollama_num_ctx: int = field(default_factory=lambda: _int("OLLAMA_NUM_CTX", 24576))

    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.getenv("CLAUDE_EFFORT", "medium"))

    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "small"))
    whisper_threads: int = field(default_factory=lambda: _int("WHISPER_THREADS", os.cpu_count() or 4))

    clip_min_seconds: int = field(default_factory=lambda: _int("CLIP_MIN_SECONDS", 60))
    clip_max_seconds: int = field(default_factory=lambda: _int("CLIP_MAX_SECONDS", 120))
    # Jump-cut pauses where nobody talks and the audio is quiet.
    cut_dead_air: bool = field(default_factory=lambda: os.getenv("CUT_DEAD_AIR", "1").strip() not in ("0", "false", "no"))
    clips_per_vod: int = field(default_factory=lambda: _int("CLIPS_PER_VOD", 4))
    clip_retention_days: int = field(default_factory=lambda: _int("CLIP_RETENTION_DAYS", 7))
    render_fps: int = field(default_factory=lambda: _int("RENDER_FPS", 30))
    render_preset: str = field(default_factory=lambda: os.getenv("RENDER_PRESET", "veryfast"))

    vod_poll_minutes: int = field(default_factory=lambda: _int("VOD_POLL_MINUTES", 30))
    clipper_poll_minutes: int = field(default_factory=lambda: _int("CLIPPER_POLL_MINUTES", 60))
    reference_poll_minutes: int = field(default_factory=lambda: _int("REFERENCE_POLL_MINUTES", 180))

    # Public URL of the built-in file server, e.g. http://203.0.113.5:8080 or https://clips.example.com
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    file_server_port: int = field(default_factory=lambda: _int("FILE_SERVER_PORT", 8080))

    # Netscape-format cookies file for yt-dlp (needed for Instagram and often for YouTube on cloud IPs).
    ytdlp_cookies: str = field(default_factory=lambda: os.getenv("YTDLP_COOKIES", ""))

    yunet_model: str = field(default_factory=lambda: os.getenv("YUNET_MODEL", "./assets/face_detection_yunet.onnx"))
    fonts_dir: str = field(default_factory=lambda: os.getenv("FONTS_DIR", "./assets/fonts"))
    subtitle_font: str = field(default_factory=lambda: os.getenv("SUBTITLE_FONT", "Montserrat ExtraBold"))

    rater_min_reference_views: int = field(default_factory=lambda: _int("RATER_MIN_REFERENCE_VIEWS", 100_000))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "streambot.sqlite3"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    def provider_for(self, purpose: str = "general") -> str:
        """AI provider for a purpose: "rating" (rater bot) or anything else (clipper)."""
        if purpose == "rating" and self.rater_llm_provider:
            return self.rater_llm_provider
        return self.llm_provider

    def llm_available(self, purpose: str = "general") -> bool:
        provider = self.provider_for(purpose)
        if provider == "anthropic":
            return bool(self.anthropic_api_key)
        return provider == "ollama"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.clips_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
