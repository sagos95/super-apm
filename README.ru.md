# super-apm

[![English](https://img.shields.io/badge/English-57606A?style=flat&logo=googletranslate&logoColor=white)](README.md)
[![Русский](https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-0969DA?style=flat&logo=googletranslate&logoColor=white)](README.ru.md)

Оболочка над [Microsoft APM](https://github.com/microsoft/apm) для общих и локальных скиллов и MCP-серверов в Claude Code и Codex.

- **Shared — общие:** список пакетов в `apm.yml`, хранится в Git вместе с проектом.
- **Local — локальные:** пакеты в `.apm-local/`, автоматически исключены из Git вместе с созданными ссылками и конфигами.

## Установка

Нужны Python 3.11+, Git, uv и APM на PATH. Проверено с APM 0.32.0.

```sh
uv tool install 'git+https://github.com/sagos95/super-apm.git@v0.2.1'
```

## Использование

Запускайте из своего проекта:

```sh
super-apm install owner/package          # добавить общий пакет
super-apm install owner/package --local  # добавить локальный пакет
super-apm install                        # установить оба набора
super-apm update --yes                   # обновить оба набора
super-apm list                           # показать все пакеты
super-apm status                         # проверить ссылки и фоновое обновление
```

Добавьте `--shared` или `--local`, чтобы работать только с одним набором. Автоматических коммитов и пушей нет.

## Автообновление

```sh
super-apm hooks install --shared-policy daily
```

При старте сессии агента пакеты обновляются в фоне раз в сутки. Чтобы сохранить закреплённые общие версии, используйте `--shared-policy pinned`; локальные пакеты продолжат обновляться ежедневно.

Одобрите изменённые хуки в Codex и перезапустите сессию агента.

## Подробнее

[Подробное руководство на английском](docs/advanced.md): настройка MCP, дополнительные команды, восстановление и разработка. Для локальных MCP нужны конфиги проекта, исключённые из Git.

Сам super-apm обновляется повторной установкой: добавьте `--force` и укажите нужный тег версии.

[Лицензия MIT](LICENSE).
