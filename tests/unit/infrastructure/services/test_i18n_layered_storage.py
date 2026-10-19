from pathlib import Path

from src.infrastructure.services.i18n import LayeredFileStorage

DEFAULT_BUTTONS = """\
btn-menu =
    .connect = 🚀 Подключиться
    .devices = 📱 Устройства
    .connect-not-available = ⚠️ ПОДПИСКА НЕ РАБОТАЕТ

brand = Clean VPN
greeting = Добро пожаловать в { brand }
"""


def _storage(tmp_path: Path, custom: str | None) -> LayeredFileStorage:
    default_dir = tmp_path / "assets.default" / "translations"
    user_dir = tmp_path / "assets" / "translations"
    (default_dir / "ru").mkdir(parents=True)
    (user_dir / "ru").mkdir(parents=True)
    (default_dir / "ru" / "buttons.ftl").write_text(DEFAULT_BUTTONS, "utf8")
    if custom is not None:
        (user_dir / "ru" / "custom.ftl").write_text(custom, "utf8")
    return LayeredFileStorage(user_translations_dir=user_dir, default_translations_dir=default_dir)


def _get(storage: LayeredFileStorage, key: str) -> str:
    (translator,) = storage.get_translators_for_language("ru")
    return str(translator.get(key))


def test_partial_message_override_keeps_default_attributes(tmp_path: Path) -> None:
    # an operator's custom.ftl copied btn-menu from a release without connect-not-available
    storage = _storage(
        tmp_path,
        "btn-menu =\n    .connect = 🔌 Моё подключение\n    .devices = 📱 Мои устройства\n",
    )

    assert _get(storage, "btn-menu.connect") == "🔌 Моё подключение"
    assert _get(storage, "btn-menu.devices") == "📱 Мои устройства"
    assert _get(storage, "btn-menu.connect-not-available") == "⚠️ ПОДПИСКА НЕ РАБОТАЕТ"


def test_custom_only_attributes_are_added(tmp_path: Path) -> None:
    storage = _storage(tmp_path, "btn-menu =\n    .custom-extra = ✨ Своё\n")

    assert _get(storage, "btn-menu.custom-extra") == "✨ Своё"
    assert _get(storage, "btn-menu.connect") == "🚀 Подключиться"


def test_overrides_stay_visible_to_message_references(tmp_path: Path) -> None:
    storage = _storage(tmp_path, "brand = Моя Марка\n")

    assert _get(storage, "greeting") == "Добро пожаловать в Моя Марка"


def test_defaults_are_used_without_custom_ftl(tmp_path: Path) -> None:
    storage = _storage(tmp_path, None)

    assert _get(storage, "btn-menu.connect-not-available") == "⚠️ ПОДПИСКА НЕ РАБОТАЕТ"
    assert _get(storage, "greeting") == "Добро пожаловать в Clean VPN"
