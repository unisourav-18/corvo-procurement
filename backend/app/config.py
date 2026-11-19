from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./corvo_procurement.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    data_dir: str = "./data"

    class Config:
        env_file = ".env"


settings = Settings()