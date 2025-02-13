import threading
from queue import Queue
from botocore.config import Config
import numpy as np
import multiprocessing
import sys

if sys.platform == "darwin":
    multiprocessing.set_start_method("fork", force=True)

__pdoc__ = {
    "core": False,
    "api": False,
    "cli": False,
    "client": False,
    "constants": False,
    "config": False,
    "integrations": False,
    "tests": False,
    "Dataset.clear_cache": False,
    "Dataset.delete": False,
    "Dataset.flush": False,
    "Dataset.read_only": False,
    "Dataset.size_approx": False,
    "Dataset.token": False,
    "Dataset.num_samples": False,
}
from .api.dataset import dataset
from .api.read import read
from .core.dataset import Dataset
from .core.transform import compute, compose
from .core.tensor import Tensor
from .util.bugout_reporter import hub_reporter
from .compression import SUPPORTED_COMPRESSIONS
from .htype import HTYPE_CONFIGURATIONS
from .integrations import huggingface

compressions = list(SUPPORTED_COMPRESSIONS)
htypes = sorted(list(HTYPE_CONFIGURATIONS))
list = dataset.list
load = dataset.load
empty = dataset.empty
like = dataset.like
delete = dataset.delete
dataset_cl = Dataset
ingest = dataset.ingest
ingest_kaggle = dataset.ingest_kaggle
ingest_huggingface = huggingface.ingest_huggingface
tensor = Tensor

__all__ = [
    "dataset",
    "tensor",
    "read",
    "__version__",
    "load",
    "empty",
    "compute",
    "compose",
    "like",
    "list",
    "dataset_cl",
    "ingest",
    "ingest_kaggle",
    "ingest_huggingface",
    "compressions",
    "htypes",
    "config",
    "delete",
]

__version__ = "2.2.3"
__encoded_version__ = np.array(__version__)
config = {"s3": Config(max_pool_connections=50)}


hub_reporter.tags.append(f"version:{__version__}")
hub_reporter.system_report(publish=True)
hub_reporter.setup_excepthook(publish=True)

event_queue: Queue = Queue()


def send_event():
    while True:
        try:
            event = event_queue.get()
            client, event_dict = event
            client.send_event(event_dict)
        except Exception:
            pass


threading.Thread(target=send_event, daemon=True).start()
