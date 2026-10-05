"""Order execution (PRD §18, §21) behind the common execution interface (PRD v3 §14)."""

from .broker_api import PLATFORMS, ExecutionBroker, MT5ExecutionAdapter, execution_adapter
from .executor import EntryOrder, ExecResult, Executor, client_order_id
from .settings import ExecutionSettings, load_execution_settings

__all__ = ["PLATFORMS", "EntryOrder", "ExecResult", "ExecutionBroker", "ExecutionSettings", "Executor",
           "MT5ExecutionAdapter", "client_order_id", "execution_adapter", "load_execution_settings"]
