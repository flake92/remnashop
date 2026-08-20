"""Narrow compatibility fixes for the pinned Remnawave SDK contract.

Keep these adjustments version-agnostic and idempotent so they become no-ops
as soon as remnapy declares the response fields optional itself.
"""

from loguru import logger
from remnapy.models.hosts import (
    CreateHostResponseDto,
    GetAllHostsResponseDto,
    GetOneHostResponseDto,
    HostResponseDto,
    HostsResponseDto,
    UpdateHostResponseDto,
)

_HOST_RESPONSE_MODELS = (
    HostResponseDto,
    CreateHostResponseDto,
    UpdateHostResponseDto,
    GetOneHostResponseDto,
    HostsResponseDto,
)
_XHTTP_FIELD = "xhttp_extra_params"
_XHTTP_ALIAS = "xhttpExtraParams"


def apply_remnapy_contract_compatibility() -> None:
    """Accept Remnawave 2.8 host responses that omit xhttpExtraParams."""

    patched = False
    for model in _HOST_RESPONSE_MODELS:
        field = model.model_fields.get(_XHTTP_FIELD)
        if field is None or field.alias != _XHTTP_ALIAS:
            raise RuntimeError(f"Unsupported remnapy {model.__name__}.{_XHTTP_FIELD} contract")
        if not field.is_required():
            continue

        field.default = None
        model.model_rebuild(force=True)
        patched = True

    # The list response embeds HostResponseDto's compiled validator and must be
    # rebuilt after the item model changes.
    if patched:
        GetAllHostsResponseDto.model_rebuild(force=True)
        logger.info("Applied remnapy compatibility for optional host xhttpExtraParams")
