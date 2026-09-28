"""OmniSight shared data contracts.

Import from this package, never from submodules, so internal layout can change
without touching the server, client, or tests.
"""

from .markdown import derive_summary, extract_code_blocks, normalize_language, strip_code_blocks
from .models import (
    CONTRACT_VERSION,
    DEFAULT_TEMPERATURE,
    ERROR_HTTP_STATUS,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_MS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_NEW_TOKENS,
    MAX_PROMPT_CHARS,
    AnalysisMode,
    AnalyzeRequest,
    AnalyzeResponse,
    AudioPayload,
    ClientInfo,
    CodeBlock,
    EndpointRecord,
    ErrorCode,
    ErrorResponse,
    HealthResponse,
    ImagePayload,
    InferenceTimings,
    decode_base64_strict,
    encode_base64,
    sniff_image_mime,
)

__version__ = CONTRACT_VERSION

__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_TEMPERATURE",
    "ERROR_HTTP_STATUS",
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_DURATION_MS",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_NEW_TOKENS",
    "MAX_PROMPT_CHARS",
    "AnalysisMode",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "AudioPayload",
    "ClientInfo",
    "CodeBlock",
    "EndpointRecord",
    "ErrorCode",
    "ErrorResponse",
    "HealthResponse",
    "ImagePayload",
    "InferenceTimings",
    "__version__",
    "decode_base64_strict",
    "derive_summary",
    "encode_base64",
    "extract_code_blocks",
    "normalize_language",
    "sniff_image_mime",
    "strip_code_blocks",
]
