# MaintainerLens documentation

[English project overview](../README.md) · [中文项目介绍](../README.zh-CN.md)

## Install and use

| Guide | What you will find |
| --- | --- |
| [Installation](INSTALLATION.md) | Requirements, one-command startup, service lifecycle, upgrades and host-mode setup |
| [Configuration](CONFIGURATION.md) | Model protocols, `.env` fields, database/broker credentials, ports and budgets |
| [Web and API usage](WEB_INTERFACE.md) | Issue submission, reports, language selection, URL reuse and review decisions |
| [Troubleshooting](TROUBLESHOOTING.md) | Find tasks by URL/ID, inspect phases/errors and read/export Docker logs |
| [Supported languages](SUPPORTED_LANGUAGES.md) | Exact file allowlist, syntax-aware versus text-only coverage and exclusions |

## Understand and contribute

| Guide | What you will find |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Components, technology choices, data flow and transaction/recovery boundaries |
| [Development and tests](DEVELOPMENT.md) | Local tooling, test isolation, CI and contribution workflow |
| [Report evaluation](REPORT_QUALITY.md) | Retrieval metrics, human review and opt-in deployed acceptance |
| [Roadmap](ROADMAP.md) | Implemented V1 scope and proposed V2 work |
| [Architecture decisions](ADR/README.md) | Public decision records and their tradeoffs |

The documentation describes current behavior. Architectural decisions retain
their historical context; they do not replace the implementation or current
operating instructions. Development diaries and individual acceptance sessions
are not part of the public user guide.
