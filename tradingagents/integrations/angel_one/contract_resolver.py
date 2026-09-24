"""
Dynamic Indian F&O Contract Resolver.
Resolves directional AI signals and spot prices into exact, tradable exchange contracts.
Adheres strictly to fail-closed invariants and dynamically discovers strikes, expiries, and lot sizes.
"""

import logging
from typing import Dict, List, Optional

from .models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    MarketBias,
    OptionType,
    ResolutionRequest,
    ResolutionResult,
    StrikeMode,
)
from .scrip_master import ScripMasterManager

logger = logging.getLogger(__name__)

# Default index-to-exchange mapping
DEFAULT_INDEX_EXCHANGE: Dict[str, Exchange] = {
    "NIFTY": Exchange.NFO,
    "BANKNIFTY": Exchange.NFO,
    "FINNIFTY": Exchange.NFO,
    "MIDCPNIFTY": Exchange.NFO,
    "NIFTYMIDCAP100": Exchange.NFO,
    "SENSEX": Exchange.BFO,
    "BANKEX": Exchange.BFO,
}


class FOContractResolver:
    """Resolves abstract trading signals into exact Angel One F&O contracts."""

    def __init__(self, scrip_master: ScripMasterManager):
        self.scrip_master = scrip_master

    def determine_exchange(self, underlying: str) -> Optional[Exchange]:
        """Determine exchange (NFO/BFO) for an underlying dynamically or via mapping."""
        u_upper = underlying.upper().strip()
        if u_upper in DEFAULT_INDEX_EXCHANGE:
            return DEFAULT_INDEX_EXCHANGE[u_upper]

        # Dynamic search in Scrip Master across derivative segments
        for exch in (Exchange.NFO, Exchange.BFO):
            underlyings = self.scrip_master.get_supported_underlyings(exch.value)
            if u_upper in underlyings:
                return exch

        return None

    def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        """
        Resolve a ResolutionRequest into a concrete ContractSpec.
        Enforces FAIL-CLOSED semantics on any ambiguity, error, or missing data.
        """
        underlying = request.underlying.upper().strip()
        spot_price = request.spot_price

        # 1. Exchange Resolution
        exchange = self.determine_exchange(underlying)
        if exchange is None:
            return ResolutionResult(
                success=False,
                error_reason=f"Unknown instrument: '{underlying}' not found in supported derivative segments.",
            )

        # 2. Expiry Discovery (Dynamic, filtered for unexpired dates)
        expiries = self.scrip_master.get_expiries(
            underlying=underlying,
            exchange=exchange.value,
            instrument_type=request.instrument_type.value,
            filter_unexpired=True,
        )

        if not expiries:
            return ResolutionResult(
                success=False,
                error_reason=f"No valid unexpired contracts found for {underlying} ({exchange.value}). Stale/expired contracts rejected.",
            )

        # Select target expiry
        selected_expiry = expiries[0]  # Nearest valid unexpired expiry by default
        if request.preferred_expiry:
            pref_parsed = self.scrip_master.parse_expiry_date(request.preferred_expiry)
            matched = False
            for exp in expiries:
                if self.scrip_master.parse_expiry_date(exp) == pref_parsed:
                    selected_expiry = exp
                    matched = True
                    break
            if not matched:
                return ResolutionResult(
                    success=False,
                    error_reason=f"Preferred expiry '{request.preferred_expiry}' is invalid, expired, or unavailable for {underlying}.",
                )

        # 3. Instrument-Specific Resolution
        if request.instrument_type == DerivativeType.OPTIDX:
            return self._resolve_option(
                request=request,
                underlying=underlying,
                exchange=exchange,
                expiry=selected_expiry,
                spot_price=spot_price,
            )
        elif request.instrument_type == DerivativeType.FUTIDX:
            return self._resolve_future(
                underlying=underlying,
                exchange=exchange,
                expiry=selected_expiry,
            )
        else:
            return ResolutionResult(
                success=False,
                error_reason=f"Unsupported instrument type: {request.instrument_type}",
            )

    def _resolve_option(
        self,
        request: ResolutionRequest,
        underlying: str,
        exchange: Exchange,
        expiry: str,
        spot_price: float,
    ) -> ResolutionResult:
        """Resolve an index option contract."""
        # Validate Market Bias
        if request.bias == MarketBias.NEUTRAL:
            return ResolutionResult(
                success=False,
                error_reason="NEUTRAL bias cannot select directional option (CE/PE). Fail-closed state: NO TRADE.",
            )

        # Directional mapping: Bullish -> CE, Bearish -> PE
        option_type = OptionType.CE if request.bias == MarketBias.BULLISH else OptionType.PE

        # Dynamically discover all available strikes from Scrip Master for this expiry
        available_strikes = self.scrip_master.get_available_strikes(
            underlying=underlying,
            exchange=exchange.value,
            expiry=expiry,
            option_type=option_type.value,
        )

        if not available_strikes:
            return ResolutionResult(
                success=False,
                error_reason=f"No available strikes discovered for {underlying} {expiry} {option_type.value}.",
            )

        # Find ATM strike index: strike closest to current spot price
        atm_idx = min(range(len(available_strikes)), key=lambda i: abs(available_strikes[i] - spot_price))

        # Calculate target strike index based on StrikeMode and Offset
        offset = request.strike_offset
        if request.strike_mode == StrikeMode.ATM:
            target_idx = atm_idx
        elif request.strike_mode == StrikeMode.ITM:
            # For CE: ITM is lower strike (index decreases)
            # For PE: ITM is higher strike (index increases)
            target_idx = atm_idx - offset if option_type == OptionType.CE else atm_idx + offset
        elif request.strike_mode == StrikeMode.OTM:
            # For CE: OTM is higher strike (index increases)
            # For PE: OTM is lower strike (index decreases)
            target_idx = atm_idx + offset if option_type == OptionType.CE else atm_idx - offset
        else:
            return ResolutionResult(
                success=False,
                error_reason=f"Unknown strike mode: {request.strike_mode}",
            )

        # Boundary check
        if target_idx < 0 or target_idx >= len(available_strikes):
            return ResolutionResult(
                success=False,
                error_reason=f"Requested strike offset {offset} out of bounds. Available strike count: {len(available_strikes)}.",
                available_strikes=available_strikes,
                resolved_expiry=expiry,
            )

        target_strike = available_strikes[target_idx]

        # Fetch the exact contract record from Scrip Master
        contract_rec = self.scrip_master.find_option_contract(
            underlying=underlying,
            exchange=exchange.value,
            expiry=expiry,
            strike=target_strike,
            option_type=option_type.value,
        )

        if contract_rec is None:
            return ResolutionResult(
                success=False,
                error_reason=f"Contract not found in Scrip Master for {underlying} {expiry} {target_strike} {option_type.value}.",
                available_strikes=available_strikes,
                resolved_expiry=expiry,
            )

        # Strict validation of token and lot size
        if not contract_rec.token:
            return ResolutionResult(
                success=False,
                error_reason=f"Missing instrument token for contract {contract_rec.symbol}.",
            )

        if contract_rec.lotsize <= 0:
            return ResolutionResult(
                success=False,
                error_reason=f"Invalid lot size ({contract_rec.lotsize}) for contract {contract_rec.symbol}.",
            )

        contract_spec = ContractSpec(
            underlying=underlying,
            exchange=exchange,
            trading_symbol=contract_rec.symbol,
            symbol_token=contract_rec.token,
            instrument_type=DerivativeType.OPTIDX,
            option_type=option_type,
            strike_price=target_strike,
            expiry_date=expiry,
            lot_size=contract_rec.lotsize,
            tick_size=contract_rec.tick_size,
        )

        return ResolutionResult(
            success=True,
            contract=contract_spec,
            available_strikes=available_strikes,
            resolved_expiry=expiry,
        )

    def _resolve_future(
        self,
        underlying: str,
        exchange: Exchange,
        expiry: str,
    ) -> ResolutionResult:
        """Resolve an index futures contract."""
        contract_rec = self.scrip_master.find_future_contract(
            underlying=underlying,
            exchange=exchange.value,
            expiry=expiry,
        )

        if contract_rec is None:
            return ResolutionResult(
                success=False,
                error_reason=f"Futures contract not found for {underlying} {expiry} on {exchange.value}.",
                resolved_expiry=expiry,
            )

        if not contract_rec.token:
            return ResolutionResult(
                success=False,
                error_reason=f"Missing instrument token for futures contract {contract_rec.symbol}.",
            )

        if contract_rec.lotsize <= 0:
            return ResolutionResult(
                success=False,
                error_reason=f"Invalid lot size ({contract_rec.lotsize}) for futures contract {contract_rec.symbol}.",
            )

        contract_spec = ContractSpec(
            underlying=underlying,
            exchange=exchange,
            trading_symbol=contract_rec.symbol,
            symbol_token=contract_rec.token,
            instrument_type=DerivativeType.FUTIDX,
            expiry_date=expiry,
            lot_size=contract_rec.lotsize,
            tick_size=contract_rec.tick_size,
        )

        return ResolutionResult(
            success=True,
            contract=contract_spec,
            resolved_expiry=expiry,
        )

