"""
NEXUS Domain Agents
====================
All 7 domain agents + their base class.

Each agent follows the same contract:
    sense() → List[IncidentEvent]   # observe → return events
    run()                           # base class: call sense() on schedule, publish to NATS

Import examples:
    from nexus.agents import MetricsAgent, GitAgent, K8sAgent
    from nexus.agents import DBAgent, NetworkAgent, ConfigAgent, NginxAgent
"""

from nexus.agents.base_agent    import BaseAgent
from nexus.agents.metrics_agent import MetricsAgent
from nexus.agents.git_agent     import GitAgent, EnvContractValidator
from nexus.agents.k8s_agent     import K8sAgent
from nexus.agents.db_agent      import DBAgent, PostgresAdapter, MySQLAdapter, MongoDBAdapter, db_agent_from_env
from nexus.agents.network_agent import NetworkAgent
from nexus.agents.config_agent  import ConfigAgent
from nexus.agents.nginx_agent   import NginxAgent

__all__ = [
    "BaseAgent",
    "MetricsAgent",
    "GitAgent",
    "EnvContractValidator",
    "K8sAgent",
    "DBAgent",
    "PostgresAdapter",
    "MySQLAdapter",
    "MongoDBAdapter",
    "db_agent_from_env",
    "NetworkAgent",
    "ConfigAgent",
    "NginxAgent",
]
