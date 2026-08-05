"""Unit tests for the centralized config module."""

from ppa.config import (
    CLIConfig,
    Config,
    ModelConfig,
    OperatorConfig,
    PrometheusConfig,
    ScalingConfig,
    get_config,
    reset_config,
    set_config,
)


class TestPrometheusConfig:
    def test_from_env_defaults(self):
        config = PrometheusConfig.from_env()
        assert config.url == "http://prometheus:9090"
        assert config.timeout_seconds == 2
        assert config.circuit_breaker_threshold == 10

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("PROMETHEUS_URL", "http://custom:9090")
        monkeypatch.setenv("PROMETHEUS_TIMEOUT", "5")
        config = PrometheusConfig.from_env()
        assert config.url == "http://custom:9090"
        assert config.timeout_seconds == 5


class TestOperatorConfig:
    def test_from_env_defaults(self):
        config = OperatorConfig.from_env()
        assert config.namespace == "default"
        assert config.timer_interval == 30
        assert config.initial_delay == 60
        assert config.stabilization_steps == 2
        assert config.stabilization_tolerance == 0.5

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("PPA_NAMESPACE", "custom-ns")
        monkeypatch.setenv("PPA_TIMER_INTERVAL", "60")
        config = OperatorConfig.from_env()
        assert config.namespace == "custom-ns"
        assert config.timer_interval == 60


class TestScalingConfig:
    def test_from_env_defaults(self):
        config = ScalingConfig.from_env()
        assert config.min_replicas == 2
        assert config.max_replicas == 20
        assert config.scale_up_rate == 2.0
        assert config.scale_down_rate == 0.5
        assert config.capacity_per_pod == 50

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("PPA_MIN_REPLICAS", "5")
        monkeypatch.setenv("PPA_MAX_REPLICAS", "50")
        config = ScalingConfig.from_env()
        assert config.min_replicas == 5
        assert config.max_replicas == 50


class TestConfig:
    def test_from_env_creates_all_subconfigs(self):
        config = Config.from_env()
        assert isinstance(config.prometheus, PrometheusConfig)
        assert isinstance(config.operator, OperatorConfig)
        assert isinstance(config.model, ModelConfig)
        assert isinstance(config.scaling, ScalingConfig)
        assert isinstance(config.cli, CLIConfig)

    def test_get_config_returns_same_instance(self):
        reset_config()
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2

    def test_set_config_overrides_global(self, test_config):
        reset_config()
        set_config(test_config)
        assert get_config() is test_config
        reset_config()

    def test_to_dict(self):
        config = Config.from_env()
        d = config.to_dict()
        assert "prometheus" in d
        assert "operator" in d
        assert "model" in d
        assert "scaling" in d
