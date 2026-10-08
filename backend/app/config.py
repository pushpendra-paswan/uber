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

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
