from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    postgres_port: int
    redis_host: str
    redis_port: int
    redis_db: int = 0  # tests use 1, so they never touch dev data
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    nominatim_url: str
    nominatim_user_agent: str
    osrm_url: str
    city_name: str
    city_center_lat: float
    city_center_lng: float
    map_zoom: int
    city_south: float
    city_west: float
    city_north: float
    city_east: float
    # Payments (M5.3). Only Stripe TEST keys are used (sk_test_ or rk_test_); anything else counts as not configured.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_url: str = "https://api.stripe.com"
    app_base_url: str = "http://localhost:8000"

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
