from pathlib import Path

from fluent.syntax import FluentParser, FluentSerializer, ast
from fluent_compiler.bundle import FluentBundle
from fluentogram.storage.base import BaseStorage
from fluentogram.translator import FluentTranslator


class LayeredFileStorage(BaseStorage):
    def __init__(
        self,
        user_translations_dir: Path,
        default_translations_dir: Path,
        use_isolating: bool = False,
    ) -> None:
        super().__init__()
        self._user_dir = user_translations_dir
        self._default_dir = default_translations_dir
        self._use_isolating = use_isolating
        self._default_translators: dict[str, FluentTranslator] = {}
        self._load_translations()

    def _make_translator(self, locale: str, texts: list[str]) -> FluentTranslator:
        return FluentTranslator(
            locale=locale,
            translator=FluentBundle.from_string(  # type: ignore[no-untyped-call]
                locale=locale,
                text="\n".join(texts),
                use_isolating=self._use_isolating,
            ),
        )

    def _merge_texts(self, default_texts: list[str], custom_text: str | None) -> list[str]:
        """Merge custom.ftl into the default texts as a single bundle.

        Custom keys must live in the same bundle as the defaults, otherwise Fluent
        message references (``{ key }``) — which only resolve within their own bundle —
        never see the overrides. Default definitions of overridden ids are stripped so
        no duplicate-message junk is produced.

        An override replaces only what it defines: attributes (``btn-menu.connect``)
        and the value are taken from custom.ftl, while every default attribute it does
        not mention is kept. Operators often copied a whole message such as
        ``btn-menu`` from an older release; replacing it wholesale dropped attributes
        added later and raised KeyNotFoundError for them.
        """
        if not custom_text:
            return default_texts

        custom_ast = FluentParser().parse(custom_text)
        custom_entries: dict[tuple[type, str], ast.Message | ast.Term] = {
            (type(entry), entry.id.name): entry
            for entry in custom_ast.body
            if isinstance(entry, (ast.Message, ast.Term))
        }

        default_ast = FluentParser().parse("\n".join(default_texts))
        kept_defaults: list[ast.EntryType] = []
        for entry in default_ast.body:
            if not isinstance(entry, (ast.Message, ast.Term)):
                kept_defaults.append(entry)
                continue
            override = custom_entries.get((type(entry), entry.id.name))
            if override is None:
                kept_defaults.append(entry)
                continue
            if isinstance(override, ast.Message) and override.value is None:
                override.value = entry.value
            overridden_attributes = {attribute.id.name for attribute in override.attributes}
            override.attributes = [
                attribute
                for attribute in entry.attributes
                if attribute.id.name not in overridden_attributes
            ] + list(override.attributes)
        default_ast.body = kept_defaults

        stripped = FluentSerializer(with_junk=False).serialize(default_ast)
        merged_custom = FluentSerializer(with_junk=True).serialize(custom_ast)
        return [stripped, merged_custom]

    def _load_translations(self) -> None:
        # Local dev fallback: assets.default/ not present — behave like FileStorage
        if not self._default_dir.exists():
            for locale_dir in self._user_dir.iterdir():
                if not locale_dir.is_dir():
                    continue
                locale = locale_dir.name
                default_texts = [
                    f.read_text("utf8")
                    for f in sorted(locale_dir.rglob("*.ftl"))
                    if f.name != "custom.ftl"
                ]
                custom_ftl = locale_dir / "custom.ftl"
                custom_text = custom_ftl.read_text("utf8") if custom_ftl.exists() else None
                texts = self._merge_texts(default_texts, custom_text)
                if any(texts):
                    translator = self._make_translator(locale, texts)
                    self._default_translators[locale] = translator
                    self.add_translator(translator)
            return

        for locale_dir in self._default_dir.iterdir():
            if not locale_dir.is_dir():
                continue
            locale = locale_dir.name

            # Load all default .ftl files
            default_texts = [f.read_text("utf8") for f in sorted(locale_dir.rglob("*.ftl"))]

            # Load user's custom.ftl (optional) and merge into the same bundle
            custom_ftl = self._user_dir / locale / "custom.ftl"
            custom_text = custom_ftl.read_text("utf8") if custom_ftl.exists() else None

            texts = self._merge_texts(default_texts, custom_text)
            if any(texts):
                translator = self._make_translator(locale, texts)
                self._default_translators[locale] = translator
                self.add_translator(translator)

    def get_translators_for_language(self, language: str) -> list[FluentTranslator]:
        locale_chain = self._locales_map.get(language, (language,))
        result: list[FluentTranslator] = []
        for locale in locale_chain:
            if locale in self._default_translators:
                result.append(self._default_translators[locale])
        return result

    async def close(self) -> None:
        pass
