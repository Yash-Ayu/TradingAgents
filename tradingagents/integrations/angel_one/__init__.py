"""
Angel One SmartAPI Integration Package for TradingAgents.
Phase 1: Read-Only Scrip Master, Dynamic F&O Contract Resolver, and Connectivity.
"""

from .client import AngelOneClient, redact_secret
from .contract_resolver import DEFAULT_INDEX_EXCHANGE, FOContractResolver
from .models import (
    ContractSpec,
    DerivativeType,
    Exchange,
    InstrumentRecord,
    MarketBias,
    OptionType,
    ResolutionRequest,
    ResolutionResult,
    StrikeMode,
)
from .risk_engine import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskEvaluationRequest,
    RiskEvaluationResult,
)
from .paper_engine import (
    FOPaperTradingEngine,
    IndianFOChargesCalculator,
    PaperAccountStats,
    PaperOrder,
    PaperPosition,
)
from .market_data import (
    AngelOneMarketDataProvider,
    ConnectionState,
    DataSource,
    MarketTick,
)
from .orchestrator import (
    FOPipelineOrchestrator,
    PhoneAlertDispatcher,
    PipelineExecutionResult,
)
from .scrip_master import ScripMasterManager
from .session_manager import (
    MarketSessionObservability,
    PaperSessionConfig,
    SessionRecoveryManager,
    SessionStartupChecker,
    SessionStatus,
    StartupCheckResult,
)
from .signal_adapter import (
    TradingAgentsSignalAdapter,
    ValidatedSignal,
)

__all__ = [
    "AngelOneClient",
    "redact_secret",
    "ScripMasterManager",
    "FOContractResolver",
    "DEFAULT_INDEX_EXCHANGE",
    "ContractSpec",
    "DerivativeType",
    "Exchange",
    "InstrumentRecord",
    "MarketBias",
    "OptionType",
    "ResolutionRequest",
    "ResolutionResult",
    "StrikeMode",
    "DeterministicRiskEngine",
    "RiskConfig",
    "RiskEvaluationRequest",
    "RiskEvaluationResult",
    "FOPaperTradingEngine",
    "IndianFOChargesCalculator",
    "PaperAccountStats",
    "PaperOrder",
    "PaperPosition",
    "FOPipelineOrchestrator",
    "PhoneAlertDispatcher",
    "PipelineExecutionResult",
    "AngelOneMarketDataProvider",
    "ConnectionState",
    "DataSource",
    "MarketTick",
    "TradingAgentsSignalAdapter",
    "ValidatedSignal",
    "PaperSessionConfig",
    "SessionStatus",
    "StartupCheckResult",
    "SessionStartupChecker",
    "SessionRecoveryManager",
    "MarketSessionObservability",
]

