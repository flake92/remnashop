from uuid import UUID

from remnapy.models.hosts import GetAllHostsResponseDto, HostResponseDto
from remnapy.models.hwid import HwidDeviceDto
from remnapy.models.webhook import WebhookPayloadDto

from src.infrastructure.remnapy_compat import apply_remnapy_contract_compatibility


def _host_payload() -> dict:
    return {
        "uuid": "c173c271-8756-4ac3-a235-ae4c009dd886",
        "viewPosition": 0,
        "remark": "contract-check",
        "address": "vpn.example.com",
        "port": 443,
        "path": None,
        "sni": None,
        "host": None,
        "alpn": None,
        "fingerprint": None,
        "muxParams": None,
        "sockoptParams": None,
        "inbound": {
            "configProfileUuid": None,
            "configProfileInboundUuid": None,
        },
        "serverDescription": None,
        "vlessRouteId": None,
        "shuffleHost": False,
        "mihomoX25519": False,
        "mihomoIpVersion": None,
        "nodes": [],
        "xrayJsonTemplateUuid": None,
    }


def test_host_contract_accepts_missing_optional_xhttp_params_and_tag() -> None:
    apply_remnapy_contract_compatibility()
    apply_remnapy_contract_compatibility()

    host = HostResponseDto.model_validate(_host_payload())
    hosts = GetAllHostsResponseDto.model_validate([_host_payload()])

    assert host.xhttp_extra_params is None
    assert host.tags == []
    assert hosts.root[0].xhttp_extra_params is None


def test_webhook_contract_preserves_top_level_expiration_metadata() -> None:
    payload = WebhookPayloadDto.from_dict(
        {
            "event": "custom.contract_check",
            "timestamp": "2026-08-20T10:00:00Z",
            "data": {},
            "meta": {"expiration": -24},
        }
    )

    assert payload.meta is not None
    assert payload.meta.expiration == -24


def test_hwid_contract_accepts_new_user_id_and_keeps_legacy_uuid() -> None:
    new_device = HwidDeviceDto.model_validate(
        {
            "hwid": "new-device",
            "userId": 42,
            "createdAt": "2026-08-20T10:00:00Z",
            "updatedAt": "2026-08-20T10:00:00Z",
        }
    )
    legacy_uuid = UUID("d1dc2477-01e7-4847-9400-79ae63d5a4b0")
    legacy_device = HwidDeviceDto.model_validate(
        {
            "hwid": "legacy-device",
            "userUuid": str(legacy_uuid),
            "createdAt": "2026-08-20T10:00:00Z",
            "updatedAt": "2026-08-20T10:00:00Z",
        }
    )

    assert new_device.user_id == 42
    assert new_device.user_uuid is None
    assert legacy_device.user_uuid == legacy_uuid
    assert legacy_device.user_id is None
