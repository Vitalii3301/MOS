import uuid
import datetime
import pickle
import types
from typing import Any, Dict, Optional

import numpy as np
from PIL import Image

try:
    import torch
    import torch.nn as nn
except ImportError:  # pragma: no cover - optional dependency
    torch = None
    nn = None


class Meme:
    """Basic unit of information in MOS."""

    def __init__(self, content: Any, content_type: str = "code", metadata: Optional[Dict[str, Any]] = None):
        self.id = uuid.uuid4()
        self.content = content
        self.content_type = content_type
        self.metadata = metadata or {"created": datetime.datetime.utcnow().isoformat()}
        self.fitness: float = 0.0
        self.connections: Dict[uuid.UUID, float] = {}
        self._validate_content()

    def _validate_content(self) -> None:
        if self.content_type == "code" and not isinstance(self.content, types.FunctionType):
            raise TypeError("content must be function for type 'code'")
        if self.content_type == "data" and not isinstance(self.content, dict):
            raise TypeError("content must be dict for type 'data'")
        if self.content_type == "text" and not isinstance(self.content, str):
            raise TypeError("content must be str for type 'text'")
        if self.content_type == "image" and not isinstance(self.content, Image.Image):
            raise TypeError("content must be PIL.Image.Image for type 'image'")
        if self.content_type == "model" and nn is not None and not isinstance(self.content, nn.Module):
            raise TypeError("content must be torch.nn.Module for type 'model'")

    def execute(self, env: Optional[Any] = None) -> Any:
        if self.content_type == "code":
            return self.content(env)
        if self.content_type in {"data", "text"}:
            return self.content
        if self.content_type == "image":
            return np.array(self.content)
        if self.content_type == "model" and torch is not None:
            tensor = torch.tensor(env, dtype=torch.float32) if env is not None else None
            return self.content(tensor) if tensor is not None else None
        return None

    def mutate(self) -> None:
        if self.content_type == "data":
            for k, v in list(self.content.items()):
                if isinstance(v, (int, float)):
                    self.content[k] = v + np.random.uniform(-1, 1)
        elif self.content_type == "text":
            if self.content:
                idx = np.random.randint(0, len(self.content))
                new_char = np.random.choice(list("abcdefghijklmnopqrstuvwxyz "))
                self.content = self.content[:idx] + new_char + self.content[idx + 1 :]
        elif self.content_type == "image":
            arr = np.array(self.content)
            noise = np.random.randint(-10, 10, arr.shape, dtype=np.int16)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
            self.content = Image.fromarray(arr)
        elif self.content_type == "model" and torch is not None:
            for param in self.content.parameters():
                param.data += torch.randn_like(param) * 0.1
        # for code or unknown types do nothing

    def replicate(self) -> "Meme":
        new_content = pickle.loads(pickle.dumps(self.content))
        new_meme = Meme(new_content, self.content_type, self.metadata.copy())
        new_meme.connections = self.connections.copy()
        return new_meme

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "content_type": self.content_type,
            "metadata": self.metadata,
            "fitness": self.fitness,
            "connections": {str(k): v for k, v in self.connections.items()},
        }
