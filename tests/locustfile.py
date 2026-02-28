from locust import HttpUser, task, between

class AppUser(HttpUser):
    wait_time = between(0.5, 2)

    @task
    def get_home(self):
        self.client.get("/")
