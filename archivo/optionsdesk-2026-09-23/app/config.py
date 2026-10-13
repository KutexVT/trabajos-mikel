from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.models import Model, Mode

ROOT = Path(__file__).resolve().parent.parent


class Config(Model):
    mode: Mode = 'OBSERVE'
    symbols: list[str] = Field(default_factory=lambda: ['AAPL', 'MSFT'], min_length=1, max_length=10)
    risk_capital: float = Field(default=10000, gt=0, le=1000000)
    max_trade_risk_pct: float = Field(default=.005, gt=0, le=.01)
    max_daily_loss_pct: float = Field(default=.01, gt=0, le=.02)
    max_openings_per_day: int = Field(default=3, ge=1, le=3)
    min_dte: int = Field(default=21, ge=1, le=60)
    max_dte: int = Field(default=60, ge=1, le=90)
    max_spread_pct: float = Field(default=.15, gt=0, le=.30)
    max_data_age_seconds: int = Field(default=30, ge=1, le=60)
    cooldown_minutes: int = Field(default=15, ge=15, le=1440)
    decision_minutes: int = Field(default=5, ge=5, le=60)
    max_quote_change_pct: float = Field(default=.05, gt=0, le=.10)
    max_proposal_seconds: int = Field(default=90, ge=5, le=300)
    commission_per_contract: float = Field(default=.65, ge=0, le=20)
    slippage: float = Field(default=.01, ge=0, le=1)
    max_candidates: int = Field(default=8, ge=1, le=20)
    signal_threshold: float = Field(default=.001, gt=0, le=.1)
    strategy: str = Field(default='momentum-v1', pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    openai_model: str = Field(default='gpt-6-astra', min_length=1, max_length=100)
    openai_timeout_seconds: int = Field(default=20, ge=1, le=60)
    ibkr_host: Literal['127.0.0.1', 'localhost'] = '127.0.0.1'
    ibkr_port: int = Field(default=4002, ge=1, le=65535)
    ibkr_client_id: int = Field(default=17, ge=1, le=999)
    ibkr_timeout_seconds: int = Field(default=12, ge=2, le=60)
    fake_latency_ms: int = Field(default=10, ge=0, le=5000)
    @field_validator('symbols')
    @classmethod
    def symbols_valid(cls, values):
        values = [s.strip().upper() for s in values]
        if any(not re.fullmatch(r'[A-Z][A-Z0-9.]{0,9}', s) for s in values) or len(set(values)) != len(values):
            raise ValueError('invalid or repeated symbol')
        return values
    @model_validator(mode='after')
    def bounds(self):
        if self.max_dte < self.min_dte or 60 % self.decision_minutes:
            raise ValueError('DTE range invalid or decision interval does not divide 60')
        return self


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or ROOT / 'config.json')
    values = json.loads(path.read_text()) if path.exists() else {}
    if os.environ.get('OPENAI_MODEL'):
        values['openai_model'] = os.environ['OPENAI_MODEL']
    for key in ('IBKR_HOST', 'IBKR_PORT', 'IBKR_CLIENT_ID'):
        if key in os.environ:
            values[key.lower()] = os.environ[key] if key.endswith('HOST') else int(os.environ[key])
    return Config.model_validate(values)


def database_path() -> Path:
    return Path(os.environ.get('APP_DB', str(ROOT / 'data' / 'app.sqlite3'))).resolve()
