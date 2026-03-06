import os
from locust import HttpUser, task, between, LoadTestShape

class AppUser(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(5)
    def get_home(self):
        self.client.get("/")

    @task(2)
    def get_slow(self):
        self.client.get("/slow")

    @task(1)
    def get_error(self):
        self.client.get("/nonexistent", name="/404-generator")


class StagesShape(LoadTestShape):
    """
    Simulates phased traffic with configurable durations and scales via env vars.
    Supports FAST_MODE where 1 second of simulation is treated as 1 minute of reality.
    """
    fast_mode = os.getenv("FAST_MODE", "false").lower() == "true"
    # In fast mode, a phase specified as 60 units takes 60 seconds (1 minute). Otherwise 3600s (1 hour).
    time_multiplier = 1 if fast_mode else 60

    # Defaults: Low(1h) -> Med(2h) -> Spike(1h) -> Recovery(2h) -> Low(1h)
    stages = [
        {"duration": int(os.getenv("STAGE_LOW_DUR", 60)) * time_multiplier, "users": int(os.getenv("STAGE_LOW_USERS", 50)), "rate": float(os.getenv("STAGE_LOW_RATE", 10.0))},
        {"duration": int(os.getenv("STAGE_MED_DUR", 120)) * time_multiplier, "users": int(os.getenv("STAGE_MED_USERS", 250)), "rate": float(os.getenv("STAGE_MED_RATE", 50.0))},
        {"duration": int(os.getenv("STAGE_SPIKE_DUR", 180)) * time_multiplier, "users": int(os.getenv("STAGE_SPIKE_USERS", 1000)), "rate": float(os.getenv("STAGE_SPIKE_RATE", 200.0))},
        {"duration": int(os.getenv("STAGE_RECOVERY_DUR", 120)) * time_multiplier, "users": int(os.getenv("STAGE_RECOVERY_USERS", 250)), "rate": float(os.getenv("STAGE_RECOVERY_RATE", 50.0))},
        {"duration": int(os.getenv("STAGE_LOW_DUR_2", 60)) * time_multiplier, "users": int(os.getenv("STAGE_LOW_USERS_2", 20)), "rate": float(os.getenv("STAGE_LOW_RATE_2", 10.0))},
    ]

    def tick(self):
        run_time = self.get_run_time()
        cycle_time = sum([s["duration"] for s in self.stages])
        
        time_in_cycle = run_time % cycle_time
        
        elapsed = 0
        for stage in self.stages:
            if time_in_cycle < elapsed + stage["duration"]:
                return (stage["users"], stage["rate"])
            elapsed += stage["duration"]
            
        return (self.stages[-1]["users"], self.stages[-1]["rate"])
