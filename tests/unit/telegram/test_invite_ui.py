import ast
from pathlib import Path

from fluent_compiler.bundle import FluentBundle

TRANSLATIONS_DIR = Path("assets/translations/ru")
DIALOG_PATH = Path("src/telegram/routers/menu/dialog.py")


def _ru_bundle() -> FluentBundle:
    source = "\n".join(
        path.read_text("utf8") for path in sorted(TRANSLATIONS_DIR.glob("*.ftl"))
    )
    return FluentBundle.from_string(locale="ru", text=source, use_isolating=False)


def _call_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def test_invite_assets_render_clean_pay_guidance_and_compact_labels() -> None:
    bundle = _ru_bundle()
    rendered, errors = bundle.format(
        "msg-menu-invite",
        {
            "reward_type": "EXTRA_DAYS",
            "referral_url": "https://t.me/bot?start=ref",
            "has_web_referral_url": 1,
            "web_referral_url": "https://pay.example/invite/ref",
            "referrals": 0,
            "payments": 0,
            "points": 0,
            "empty": "",
        },
    )

    assert errors == []
    assert "Clean Pay — регистрация и оплата: https://pay.example/invite/ref" in rendered
    assert "Рекомендуется тем, у кого нет доступа к Telegram." in rendered

    expected_labels = {
        "btn-invite.about": "❓ О награде",
        "btn-invite.copy-telegram": "Telegram",
        "btn-invite.open-web": "🌐 Clean Pay",
        "btn-invite.send": "📩 Поделиться",
        "btn-invite.qr": "🧾 QR",
        "btn-invite.reset-referral": "🔄 Обновить ссылку",
    }
    for key, expected in expected_labels.items():
        label, label_errors = bundle.format(key)
        assert label_errors == []
        assert label == expected


def test_invite_dialog_uses_one_compact_link_row_without_duplicate_web_copy() -> None:
    module = ast.parse(DIALOG_PATH.read_text("utf8"))
    invite_assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "invite" for target in node.targets)
    )
    assert isinstance(invite_assignment.value, ast.Call)

    link_rows = [
        argument
        for argument in invite_assignment.value.args
        if isinstance(argument, ast.Call)
        and _call_name(argument) == "Row"
        and any(
            isinstance(widget, ast.Call) and _call_name(widget) == "CopyText"
            for widget in argument.args
        )
    ]

    assert len(link_rows) == 1
    link_row = link_rows[0]
    widgets = [
        _call_name(widget) for widget in link_row.args if isinstance(widget, ast.Call)
    ]
    assert widgets == ["CopyText", "Url"]

    url_widget = link_row.args[1]
    assert isinstance(url_widget, ast.Call)
    assert any(keyword.arg == "when" for keyword in url_widget.keywords)
