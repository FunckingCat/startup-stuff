# Лог

Append-only. Формат заголовка записи фиксирован, чтобы лог парсился:
`## [ГГГГ-ММ-ДД] <операция> | <название>`, где операция — `ingest`, `query`, `lint`, `digest`.

Последние пять записей: `grep "^## \[" log.md | tail -5`

## [2026-08-19] ingest | Do Things That Don't Scale

## [2026-08-19] ingest | Perfection By Subtraction – The Minimum Feature Set

## [2026-08-19] query | когда поднимать первый раунд

## [2026-08-19] lint | 2 дефекта, 5 дыр (правки не применены — нет подтверждения владельца)
