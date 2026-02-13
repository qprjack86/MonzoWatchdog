from core.settings import Settings
from stores.azure_table_store import AzureTableStore
from stores.memory_store import MemoryStore


def build_state_store(settings: Settings):
    backend = settings.state_backend.lower()
    if backend == "memory":
        return MemoryStore()
    if backend == "azure_table":
        return AzureTableStore(settings)
    raise ValueError(f"Unsupported STATE_BACKEND: {settings.state_backend}")
