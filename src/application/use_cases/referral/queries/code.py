import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import UserDao
from src.application.dto import UserDto
from src.core.constants import ASSETS_DIR


@dataclass(frozen=True)
class ValidateReferralCodeDto:
    user_id: int
    referral_code: str


class ValidateReferralCode(Interactor[ValidateReferralCodeDto, bool]):
    required_permission = None

    def __init__(self, user_dao: UserDao) -> None:
        self.user_dao = user_dao

    async def _execute(self, actor: UserDto, data: ValidateReferralCodeDto) -> bool:
        referrer = await self.user_dao.get_by_referral_code(data.referral_code)
        if not referrer:
            # The access middleware validates before the user is known and passes the
            # sentinel id 0, so the id is only worth logging when it is a real one.
            for_user = f" (user_id '{data.user_id}')" if data.user_id > 0 else ""
            logger.warning(f"Referral code '{data.referral_code}' not found{for_user}")
            return False
        if referrer.id == data.user_id:
            logger.warning(
                f"Self-referral rejected: user_id '{data.user_id}' used own code "
                f"'{data.referral_code}'"
            )
            return False
        return True


class GenerateReferralQr(Interactor[str, str]):
    required_permission = None

    async def _execute(self, actor: UserDto, url: str) -> str:
        from PIL import Image  # noqa: PLC0415
        from qrcode import ERROR_CORRECT_H, QRCode  # type: ignore[attr-defined]  # noqa: PLC0415

        qr: Any = QRCode(
            version=1,
            error_correction=ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )

        qr.add_data(url)
        qr.make(fit=True)

        qr_img_raw = qr.make_image(fill_color="black", back_color="white")

        if hasattr(qr_img_raw, "get_image"):
            qr_img = cast(Image.Image, qr_img_raw.get_image())
        else:
            qr_img = cast(Image.Image, qr_img_raw)

        qr_img = qr_img.convert("RGB")

        logo_path = ASSETS_DIR / "logo.png"
        if logo_path.exists():
            logo = Image.open(logo_path).convert("RGBA")

            qr_width, qr_height = qr_img.size
            logo_size = int(qr_width * 0.2)
            logo = logo.resize((logo_size, logo_size), resample=Image.Resampling.LANCZOS)

            pos = ((qr_width - logo_size) // 2, (qr_height - logo_size) // 2)
            qr_img.paste(logo, pos, mask=logo)

        buffer = BytesIO()
        qr_img.save(buffer, format="PNG")
        qr_bytes = buffer.getvalue()
        qr_base64 = base64.b64encode(qr_bytes).decode("ascii")
        buffer.seek(0)

        logger.info(f"{actor.log} Generated referral QR for URL '{url}'")

        return qr_base64
