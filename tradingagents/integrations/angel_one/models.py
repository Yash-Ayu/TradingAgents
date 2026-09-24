"""
Data models and schemas for Angel One Indian F&O integration.
Enforces strict type-safety, fail-closed validation, and immutability.
"""

from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class Exchange(str, Enum):
    NFO = "NFO"
    BFO = "BFO"
    NSE = "NSE"
    BSE = "BSE"


class DerivativeType(str, Enum):
    OPTIDX = "OPTIDX"
    FUTIDX = "FUTIDX"
    OPTSTK = "OPTSTK"
    FUTSTK = "FUTSTK"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class StrikeMode(str, Enum):
    ATM = "ATM"
    ITM = "ITM"
    OTM = "OTM"


class MarketBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class InstrumentRecord(BaseModel):
    """Raw/parsed instrument row from Angel One Scrip Master."""
    token: str
    symbol: str
    name: str
    expiry: Optional[str] = None
    strike: Optional[float] = None
    lotsize: int
    instrumenttype: str
    exch_seg: str
    tick_size: float = 0.05

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Instrument token cannot be empty")
        return v.strip()

    @field_validator("lotsize")
    @classmethod
    def validate_lotsize(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"Invalid lot size: {v}. Must be > 0")
        return v


class ContractSpec(BaseModel):
    """Fully resolved and validated derivative contract specification."""
    underlying: str
    exchange: Exchange
    trading_symbol: str
    symbol_token: str
    instrument_type: DerivativeType
    option_type: Optional[OptionType] = None
    strike_price: Optional[float] = None
    expiry_date: str
    lot_size: int = Field(gt=0, description="Lot size must be greater than zero")
    tick_size: float = 0.05

    model_config = {"frozen": True}  # Immutable for safety


class ResolutionRequest(BaseModel):
    """Input request for F&O contract resolution."""
    underlying: str
    spot_price: float = Field(gt=0, description="Spot price must be positive")
    bias: MarketBias
    instrument_type: DerivativeType = DerivativeType.OPTIDX
    strike_mode: StrikeMode = StrikeMode.ATM
    strike_offset: int = Field(default=0, ge=0, description="Offset steps for ITM/OTM (0 for ATM)")
    preferred_expiry: Optional[str] = None  # Format: YYYY-MM-DD or DDMMMYYYY


class ResolutionResult(BaseModel):
    """Outcome of contract resolution process."""
    success: bool
    contract: Optional[ContractSpec] = None
    error_reason: Optional[str] = None
    available_strikes: List[float] = Field(default_factory=list)
    resolved_expiry: Optional[str] = None
    resolved_at: datetime = Field(default_factory=datetime.now)

