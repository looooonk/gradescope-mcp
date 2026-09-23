import io
import ssl
from contextlib import redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://www.gradescope.com"


@dataclass(frozen=True)
class Settings:
    email: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self):
        if not self.email or "@" not in self.email or not self.password:
            raise ValueError("Set GRADESCOPE_EMAIL and GRADESCOPE_PASSWORD in the local .env.")
        if any(c in self.email + self.password for c in "\r\n\x00"):
            raise ValueError("Credentials contain unsupported control characters.")

    @classmethod
    def load(cls, env_file: Path):
        with redirect_stderr(io.StringIO()):
            values = dotenv_values(env_file, interpolate=False)
        return cls(values.get("GRADESCOPE_EMAIL") or "", values.get("GRADESCOPE_PASSWORD") or "")


def tls_context() -> ssl.SSLContext:
    bundle = Path("/etc/ssl/cert.pem")
    return ssl.create_default_context(cafile=str(bundle) if bundle.is_file() else None)
