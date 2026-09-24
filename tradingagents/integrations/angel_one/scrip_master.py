"""
Angel One Scrip Master manager.
Downloads, caches, and indexes the daily instrument master JSON file.
Provides fast lookups for dynamic F&O contract discovery without hardcoded assumptions.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

from .models import InstrumentRecord

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


class ScripMasterManager:
    """Manages the daily Angel One instrument master cache and indexing."""

    def __init__(self, cache_dir: Optional[Path] = None):
        if cache_dir is None:
            # Default to data_cache/ in the project root (which is git-ignored)
            base_dir = Path(__file__).resolve().parents[3]
            self.cache_dir = base_dir / "data_cache"
        else:
            self.cache_dir = Path(cache_dir)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "scrip_master.json"

        # In-memory indexes
        # Key: (exchange, name.upper(), instrumenttype.upper()) -> List[InstrumentRecord]
        self._index_by_series: Dict[tuple, List[InstrumentRecord]] = {}
        # Key: (exchange, token) -> InstrumentRecord
        self._index_by_token: Dict[tuple, InstrumentRecord] = {}
        # Set of all available underlying names per exchange
        self._underlyings_by_exchange: Dict[str, Set[str]] = {}
        self._is_loaded = False
        self._last_loaded_date: Optional[date] = None

    def is_cache_fresh(self) -> bool:
        """Check if local cache exists and was modified today."""
        if not self.cache_file.exists():
            return False
        try:
            mtime = datetime.fromtimestamp(self.cache_file.stat().st_mtime).date()
            return mtime == date.today()
        except Exception:
            return False

    def download_scrip_master(self, force: bool = False) -> Path:
        """Download latest Scrip Master JSON if cache is missing, stale, or force=True."""
        if not force and self.is_cache_fresh():
            logger.info("Scrip master cache is fresh (modified today). Skipping download.")
            return self.cache_file

        logger.info(f"Downloading Scrip Master from {SCRIP_MASTER_URL} ...")
        try:
            response = requests.get(SCRIP_MASTER_URL, timeout=60)
            response.raise_for_status()
            data = response.json()

            # Write atomically to avoid partial file corruption
            temp_file = self.cache_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            temp_file.replace(self.cache_file)

            logger.info(f"Scrip Master downloaded successfully to {self.cache_file}")
            return self.cache_file
        except Exception as e:
            logger.error(f"Failed to download Scrip Master: {e}")
            if self.cache_file.exists():
                logger.warning("Falling back to existing stale Scrip Master cache.")
                return self.cache_file
            raise RuntimeError(f"Cannot acquire Scrip Master: {e}") from e

    def load(self, force_download: bool = False) -> None:
        """Load and index the scrip master data into memory."""
        cache_path = self.download_scrip_master(force=force_download)

        logger.info(f"Loading and indexing Scrip Master from {cache_path}...")
        with open(cache_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        self._index_by_series.clear()
        self._index_by_token.clear()
        self._underlyings_by_exchange.clear()

        # Parse and index only relevant derivative and cash segments
        target_segments = {"NFO", "BFO", "NSE", "BSE"}
        count = 0

        for item in raw_data:
            exch_seg = str(item.get("exch_seg", "")).strip().upper()
            if exch_seg not in target_segments:
                continue

            token = str(item.get("token", "")).strip()
            symbol = str(item.get("symbol", "")).strip()
            name = str(item.get("name", "")).strip().upper()
            expiry = str(item.get("expiry", "")).strip()
            instrumenttype = str(item.get("instrumenttype", "")).strip().upper()

            # Strike parsing
            strike_val: Optional[float] = None
            raw_strike = item.get("strike")
            if raw_strike is not None and str(raw_strike).strip() != "":
                try:
                    s_float = float(raw_strike)
                    if s_float > 0:
                        # Angel One OpenAPI Scrip Master stores option strikes in paise (scaled by 100)
                        # e.g., NIFTY 24500 is stored as 2450000.000000
                        strike_val = s_float / 100.0
                except ValueError:
                    pass

            # Lot size parsing
            try:
                lotsize = int(float(item.get("lotsize", 1)))
                if lotsize <= 0:
                    lotsize = 1
            except (ValueError, TypeError):
                lotsize = 1

            # Tick size parsing
            try:
                tick_size = float(item.get("tick_size", 0.05))
            except (ValueError, TypeError):
                tick_size = 0.05

            try:
                record = InstrumentRecord(
                    token=token,
                    symbol=symbol,
                    name=name,
                    expiry=expiry if expiry else None,
                    strike=strike_val,
                    lotsize=lotsize,
                    instrumenttype=instrumenttype,
                    exch_seg=exch_seg,
                    tick_size=tick_size,
                )
            except Exception:
                continue

            # Indexing
            series_key = (exch_seg, name, instrumenttype)
            if series_key not in self._index_by_series:
                self._index_by_series[series_key] = []
            self._index_by_series[series_key].append(record)

            self._index_by_token[(exch_seg, token)] = record

            if exch_seg not in self._underlyings_by_exchange:
                self._underlyings_by_exchange[exch_seg] = set()
            self._underlyings_by_exchange[exch_seg].add(name)

            count += 1

        self._is_loaded = True
        self._last_loaded_date = date.today()
        logger.info(f"Indexed {count} instruments across target segments.")

    def ensure_loaded(self) -> None:
        """Ensure indexes are populated and valid for today."""
        if not self._is_loaded or self._last_loaded_date != date.today():
            self.load()

    @staticmethod
    def parse_expiry_date(expiry_str: str) -> Optional[date]:
        """
        Parse Angel One expiry string into a standard date object.
        Supported formats: DDMMMYYYY (e.g., '31OCT2024', '28NOV2024') or YYYY-MM-DD.
        """
        if not expiry_str:
            return None
        cleaned = expiry_str.strip().upper()
        for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                pass
        return None

    def get_supported_underlyings(self, exchange: str) -> Set[str]:
        """Return all distinct underlyings available for an exchange."""
        self.ensure_loaded()
        return self._underlyings_by_exchange.get(exchange.upper(), set())

    def get_expiries(
        self,
        underlying: str,
        exchange: str,
        instrument_type: str = "OPTIDX",
        filter_unexpired: bool = True,
    ) -> List[str]:
        """
        Dynamically discover sorted list of unique expiry dates for an underlying.
        Filters out expired dates if filter_unexpired is True.
        """
        self.ensure_loaded()
        key = (exchange.upper(), underlying.upper(), instrument_type.upper())
        records = self._index_by_series.get(key, [])

        expiries_map: Dict[date, str] = {}
        today = date.today()

        for rec in records:
            if not rec.expiry:
                continue
            parsed = self.parse_expiry_date(rec.expiry)
            if parsed is None:
                continue
            if filter_unexpired and parsed < today:
                continue
            if parsed not in expiries_map:
                expiries_map[parsed] = rec.expiry

        # Sort expiries chronologically
        sorted_dates = sorted(expiries_map.keys())
        return [expiries_map[d] for d in sorted_dates]

    def get_available_strikes(
        self,
        underlying: str,
        exchange: str,
        expiry: str,
        option_type: Optional[str] = None,
    ) -> List[float]:
        """
        Dynamically discover available strikes from Scrip Master for a given expiry.
        Does NOT rely on hardcoded strike steps.
        """
        self.ensure_loaded()
        key = (exchange.upper(), underlying.upper(), "OPTIDX")
        records = self._index_by_series.get(key, [])

        target_parsed = self.parse_expiry_date(expiry)
        strikes = set()

        for rec in records:
            if not rec.expiry or rec.strike is None:
                continue
            if self.parse_expiry_date(rec.expiry) != target_parsed:
                continue

            if option_type:
                opt_upper = option_type.upper()
                # Symbol usually ends with CE or PE
                if not rec.symbol.endswith(opt_upper):
                    continue

            strikes.add(rec.strike)

        return sorted(list(strikes))

    def find_option_contract(
        self,
        underlying: str,
        exchange: str,
        expiry: str,
        strike: float,
        option_type: str,
    ) -> Optional[InstrumentRecord]:
        """Find the exact option contract matching parameters."""
        self.ensure_loaded()
        key = (exchange.upper(), underlying.upper(), "OPTIDX")
        records = self._index_by_series.get(key, [])

        target_parsed = self.parse_expiry_date(expiry)
        opt_upper = option_type.upper()

        for rec in records:
            if not rec.expiry or rec.strike is None:
                continue
            if self.parse_expiry_date(rec.expiry) != target_parsed:
                continue
            # Compare strike with small epsilon for float precision
            if abs(rec.strike - strike) < 0.01:
                if rec.symbol.endswith(opt_upper):
                    return rec

        return None

    def find_future_contract(
        self,
        underlying: str,
        exchange: str,
        expiry: str,
    ) -> Optional[InstrumentRecord]:
        """Find the exact futures contract matching parameters."""
        self.ensure_loaded()
        key = (exchange.upper(), underlying.upper(), "FUTIDX")
        records = self._index_by_series.get(key, [])

        target_parsed = self.parse_expiry_date(expiry)

        for rec in records:
            if not rec.expiry:
                continue
            if self.parse_expiry_date(rec.expiry) == target_parsed:
                return rec

        return None

