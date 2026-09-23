from pathlib import Path


def main() -> None:
    hosts = Path(".venv/lib/python3.12/site-packages/remnapy/models/hosts.py")
    content = hosts.read_text()
    required = 'xhttp_extra_params: Dict[str, Any] | None = Field(alias="xhttpExtraParams")'
    optional = 'xhttp_extra_params: Dict[str, Any] | None = Field(None, alias="xhttpExtraParams")'

    if optional in content:
        return
    if content.count(required) != 1:
        raise RuntimeError("Unexpected remnapy HostResponseDto xhttpExtraParams shape")

    hosts.write_text(content.replace(required, optional, 1))


if __name__ == "__main__":
    main()
