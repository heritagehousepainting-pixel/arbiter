"""Test that the scaffold package and its modules are importable."""
from __future__ import annotations


def test_arbiter_package_importable() -> None:
    import arbiter  # noqa: F401

    assert hasattr(arbiter, "__version__")


def test_types_importable() -> None:
    from arbiter import types  # noqa: F401

    assert hasattr(types, "HorizonBucket")
    assert hasattr(types, "ConfidenceSource")
    assert hasattr(types, "OrderSide")
    assert hasattr(types, "IdeaState")
    assert hasattr(types, "DegradationLevel")
    assert hasattr(types, "bucket_for_days")


def test_config_importable() -> None:
    from arbiter import config  # noqa: F401

    assert hasattr(config, "load_config")
    assert hasattr(config, "Config")


def test_logging_setup_importable() -> None:
    from arbiter import logging_setup  # noqa: F401

    assert hasattr(logging_setup, "configure_logging")
    assert hasattr(logging_setup, "get_logger")


def test_metrics_importable() -> None:
    from arbiter import metrics  # noqa: F401

    assert hasattr(metrics, "MetricsWriter")


def test_shared_executor_importable() -> None:
    from arbiter.shared import executor  # noqa: F401

    assert hasattr(executor, "Executor")
    assert hasattr(executor, "OrderIntent")
    assert hasattr(executor, "ExecutionReport")
    assert hasattr(executor, "PositionSnapshot")
    assert hasattr(executor, "AccountSnapshot")


def test_shared_sim_executor_importable() -> None:
    from arbiter.shared import sim_executor  # noqa: F401

    assert hasattr(sim_executor, "SimExecutor")



def test_db_connection_importable() -> None:
    from arbiter.db import connection  # noqa: F401

    assert hasattr(connection, "get_connection")


def test_cli_importable() -> None:
    from arbiter import cli  # noqa: F401

    assert hasattr(cli, "app")


def test_web_importable() -> None:
    from arbiter.web import server  # noqa: F401

    assert hasattr(server, "main")
