# Шаг 3: завершение HTTP, ошибки и billing

Дата: 2026-10-04. Среда: нативный macOS 26.3 arm64, Python 3.14.6, Go 1.24.1, `build/local`. Backend и TON — тестовые; настоящие GPU, CVM и on-chain settlement не использовались.

## Что изменено

- HTTP client имеет отдельный `receive_error`: обрыв до/после headers и неполный HTTP body больше не превращаются в успешное завершение. Пустой HTTP response корректно завершается на уровне транспорта; пустой inference response отклоняется postprocessor.
- JSON проверяется как один полный документ. SSE разбирается при произвольной фрагментации, включая LF/CRLF/CR, BOM и многострочные data. `[DONE]` выдаётся только после полного HTTP framing; повторный terminal, последующие data, malformed events и отсутствие terminal означают ошибку.
- Для существующего audio endpoint сохранён терминальный `transcript.text.done`, также удерживаемый до HTTP completion. Это отдельное событие в [Audio API](https://developers.openai.com/api/reference/resources/audio), а не универсальное свойство SSE. Его обработка и шифрование проверены unit-тестами; реальный audio backend не квалифицирован, см. P3-01.
- HTTP 4xx/5xx сохраняют статус и диагностическое тело. JSON/SSE backend errors сохраняют пользовательскую причину; для encrypted requests она шифруется. TL control error содержит общую причину без содержимого модели. HTTP error body ограничен 1 MiB.
- Worker при любом неуспехе отправляет нулевое расчётное usage и не увеличивает оплаченные token counters. Usage, уже переданное внутри незавершённого SSE, не становится основанием для списания.
- Client после ошибки начатого HTTP 2xx response закрывает соединение без final chunk. Полное HTTP 4xx/5xx тело завершается нормально на транспортном уровне, но запрос учитывается как failed. Для этой сквозной гарантии обновляются worker и client; proxy/TL остаются совместимыми.

## Проверка

```bash
python3 benchmark/smoke-local.py --scenario all
go test -race benchmark/server.go benchmark/server_test.go
```

Первая команда собирает runners, `encrypt-message`, тестовый Go backend и два C++ теста, затем запускает каждый сценарий в отдельном fake-TON стеке.

Полный прогон: **23 сценария, 37 запросов, из них 9 encrypted**. Успешных запросов 12, неуспешных 25. Успех списывает 134 adjusted tokens ровно один раз; ошибка — 0, включая ошибки после usage и `[DONE]`. Суммарно списано 1608 токенов. Стенд использует ненулевой worker coefficient 1000 и fake-TON цену 2, поэтому успешный `usage.total_cost=268`: нулевая цена не маскирует ошибку billing.

Для каждого запроса сверены шесть значений: worker/proxy token counters, баланс worker и client на proxy, уведомления об оплате на worker и client. Дополнительно проверены один success/failed terminal на всех трёх ролях, освобождение reservation, отсутствие позднего повторного списания, очистка созданных process groups и освобождение портов.

| Группа сценариев | Результат |
|---|---|
| Обычные chat/completions JSON/SSE, задержки headers/body | PASS, content/usage и постепенная выдача SSE сохранены |
| Timeout, disconnect до headers, после headers и после токенов | PASS, failure и нулевая оплата |
| Неполный/пустой JSON, лишние байты после JSON | PASS, ложного success нет |
| SSE без terminal, malformed/unfinished event, duplicate terminal | PASS, ложного success и опубликованного `[DONE]` нет |
| Обрыв после usage и после `[DONE]`, но до HTTP final chunk | PASS, нулевая оплата, HTTP response оборван |
| HTTP 400/503 JSON/text/empty, HTTP 204, backend JSON/SSE error при HTTP 200 | PASS, ошибка не считается inference success |
| Encrypted normal, HTTP/JSON/SSE errors, обрыв после `[DONE]` | PASS, расшифровка и accounting корректны |

`test-http-client` проверяет 10 случаев framing/transport и ровно один terminal callback. `test-answer-postprocessor` проверяет обычные и зашифрованные ответы, разные границы фрагментов/строк, ошибки, повторное завершение и transcription terminal. Go backend tests прошли с race detector.

При финальном review добавлен отрицательный unit-тест для невалидного UTF-8 в текстовом `event: error`: сначала он воспроизвёл `json.exception.type_error.316`, затем проверка UTF-8 перевела этот случай в обычную ошибку запроса. После исправления C++ тесты пересобраны и прошли; повторный сквозной `sse-error` в обычном и encrypted режиме также прошёл с нулевой оплатой, одним failed terminal и cleanup.

## Артефакты и ограничения

Локальные логи и per-request результаты полного прогона: [`build/step3-evidence-20261004-r3`](../build/step3-evidence-20261004-r3). Повторная проверка после UTF-8 fix: [`build/step3-final-error-check-20261004`](../build/step3-final-error-check-20261004). В каждом каталоге `source-sha256.json` фиксирует соответствующие исходники. Эти каталоги находятся в ignored build tree, содержат только тестовые данные и не включены в коммит. Первые два незавершённых полных прогона сохранены как `step3-evidence-20261004` и `step3-evidence-20261004-r2`: в них обнаружена гонка готовности encryption key в стенде, исправленная явным ожиданием ключа, без retries inference.

Отмена HTTP после ошибки postprocessing и освобождение соединений остаются в шаге 4; полный предел памяти — в шаге 5; group gate/admission и member failure — в шаге 9. Новые открытые границы audio API и readiness ключей зафиксированы как P3-01/P3-02 в [плане](../pipeline-plan.md#за-скобками-реализации-шага-3). Открытые P7/P8 сохраняются. Production confidentiality и реальное GPU/KV cleanup этим отчётом не подтверждаются.
