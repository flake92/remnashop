from unittest.mock import MagicMock

from fluentogram.exceptions import KeyNotFoundError
from loguru import logger

from src.infrastructure.services.translator import TranslatorRunnerImpl


def _runner(translations: dict[str, str]) -> TranslatorRunnerImpl:
    runner = object.__new__(TranslatorRunnerImpl)
    runner.retort = MagicMock()
    runner.collapse_level = 2

    def translate(key: str, **kwargs: object) -> str:
        try:
            return translations[key].format(**kwargs)
        except KeyError as error:
            raise KeyNotFoundError(key) from error

    runner._get_translation = translate  # type: ignore[method-assign]
    return runner


def test_get_or_raw_translates_configured_key() -> None:
    runner = _runner({"menu-privacy": "🛡️ Конфиденциальность"})

    assert runner.get_or_raw("menu-privacy") == "🛡️ Конфиденциальность"


def test_get_or_raw_preserves_operator_text() -> None:
    runner = _runner({})
    records: list[dict[str, object]] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="WARNING")

    try:
        assert runner.get_or_raw("🛡️Конфиденциальность") == "🛡️Конфиденциальность"
    finally:
        logger.remove(sink_id)

    assert records == []


def test_strict_get_still_warns_for_missing_application_key() -> None:
    runner = _runner({})
    records: list[dict[str, object]] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="WARNING")

    try:
        assert runner.get("missing-application-key") == "missing-application-key"
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    assert "missing-application-key" in str(records[0]["message"])
