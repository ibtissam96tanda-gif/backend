from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    admin_token: str = "tanda-admin-change-me"
    shipping_cost_mad: int = 35

    google_sheet_id: str = ""
    google_service_account_json: str = ""
    google_service_account_file: str = ""

    sendit_public_key: str = ""
    sendit_secret_key: str = ""
    sendit_pickup_district: str = "Casablanca"
    sendit_allow_open: str = "1"
    sendit_allow_try: str = "0"

    # MaxMind GeoIP Insights
    maxmind_account_id: str = ""
    maxmind_license_key: str = ""
    maxmind_allowed_countries: str = "MA"
    maxmind_review_confidence: int = 30
    maxmind_block_confidence: int = 90
    maxmind_risk_review_threshold: float = 75.0

    # Test phone whitelist (comma-separated, bypasses fraud checks)
    test_phone_whitelist: str = "0700000000,0600000000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ORDERS_FILE = DATA_DIR / "orders.json"
LEGACY_ORDERS = DATA_DIR / "orders.jsonl"
PRODUCTS_FILE = DATA_DIR / "products.json"
