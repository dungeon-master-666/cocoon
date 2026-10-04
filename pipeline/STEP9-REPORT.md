# Шаг 9: один pipeline worker в Cocoon

Дата: 2026-10-04. Реализация проверена на macOS arm64 и в Ubuntu 24.04 ARM64 VM `cocoon-pipeline-net`, kernel `7.0.11-orbstack-00360-gc9bc4d96ac70`. Backend — simulator, TON — fake. Аппаратная конфиденциальность и настоящий GPU inference не заявляются.

## Что реализовано

`pipeline/Gate.cpp` работает в том же actor/io_context, что supervisor и протокол группы head-agent. Gate слушает только loopback, а backend получает только через UDS из launch plan текущего epoch. Member не публикует gate. В runtime разрешён лишь порт; произвольный backend address, socket или bind address задать нельзя.

Readiness — conjunction готовности группы, действующей lease, локального `LOCAL_READY` и свежего health. Здоровый `/v1/models` одного backend не открывает admission. Gate сам формирует список моделей и отдаёт 503 во всех неготовых состояниях. На каждый принятый запрос фиксируются request ID и epoch; проверка состояния повторяется в I/O callbacks. При failure закрываются все запросы epoch, до cleanup/restart backend; worker disconnect закрывает UDS даже при молчащем backend. Повторов inference нет.

Разрешены `GET /v1/models`, `POST /v1/chat/completions` и `/v1/completions`. Gate пересылает HTTP status и JSON/SSE, не считает usage и не расшифровывает сообщения. Hop-by-hop headers удаляются, framing формируется по результату Beast parser. При ошибке незавершённого upstream не публикуется final chunk. SSE terminal по-прежнему проверяет worker postprocessor.

Worker удаляет входящие headers с префиксом `x-cocoon-pipeline-`, устанавливает собственные ID и остаточный timeout, проверяет `is_disabled()` до создания actor. `/jsonstats.status.enabled` теперь отражает фактический disabled, добавлен `uplink_ok`. Штатный monitor опрашивает gate, а proxy получает прежнее enabled/disabled сообщение; TL и proxy implementation не менялись.

Simulator поддерживает chat/completions и оба token-limit aliases существующего worker. API-имя `cocoon-simulator` отделено от внутреннего идентификатора fixture `cocoon-simulator@v1:dev-fixture`; оба входят в effective config. Публичные encryption envelope fields после worker-side decryption допускаются simulator, без собственной криптографии.

Gate ограничивает request body до 8192 bytes, headers до 8192 bytes, response до 1 MiB, соединения до 16, inference до profile `max_num_seqs` (default 2), запрос до 120 с. Чтение запроса ограничено двумя секундами. На downstream последовательно пишется один chunk до 16 KiB; следующий upstream read ждёт завершения записи. Это граница самого gate, не доказательство ограниченной памяти всего Cocoon transport.

## Воспроизведение

В корне репозитория с настроенным build шага 1:

```bash
python3 test/test-pipeline-worker.py --build-dir build/local
```

В подготовленной изолированной Linux VM шага 8:

```bash
orbctl run -m cocoon-pipeline-net -u root python3 \
  /work/cocoon/test/test-pipeline-worker.py --build-dir /opt/cocoon-build --network
```

Команда собирает pipeline/worker binaries, profile test, router, cocoon-subst и encrypt-message. `--no-build` использует готовые бинарники; `--output-dir` задаёт новый каталог. По умолчанию создаётся короткий `/tmp/cp9-*`, чтобы не превысить Unix socket path limit.

Linux fixture создаёт два underlay namespace и два engine namespace с настоящим WireGuard. Cocoon client/proxy/worker/key-manager/router работают в head underlay namespace; их служебные соединения разрешены по loopback, gate подключается к изолированному engine через UDS. Root firewall VM не изменяется. Внешний служебный egress CVM не открывается и не квалифицируется.

## Результаты

**Обе среды: PASS, 17 групп проверок, 15 сквозных Cocoon-запросов в каждой.** Десять успешных запросов оплачены ровно один раз по 4 adjusted tokens, `total_cost=8` при fake-TON цене 2 и коэффициенте worker 1000. Пять неуспешных — по 0. Шесть запросов в каждой среде encrypted, включая отменяемые потоки. Суммарно на двух средах 30 запросов и 80 оплаченных adjusted tokens.

| Требование / сценарий | Наблюдаемое подтверждение |
|---|---|
| Одна группа — один worker | Proxy имеет ровно одну worker connection для `cocoon-simulator`; member не имеет gate |
| Local ready не заменяет group ready | Head backend уже отдаёт 200, member ещё стартует/прогревается; models и inference gate возвращают 503 |
| API allowlist | Health, metrics, admin, неподдерживаемые методы и query suffix отвергнуты |
| Доверенные metadata | Отсутствующие/невалидные ID/timeout отклонены; подставленные внешним HTTP-клиентом reserved headers не ломают успешный запрос через worker |
| Лимиты/повтор ID | Oversized request не достигает backend; oversized response обрывается; третий запрос получает 429, повтор ID отклоняется и при одном свободном slot; models остаётся доступным при заполненной inference capacity |
| Backend HTTP error | Gate сохраняет 503 и диагностическое тело |
| Disconnect/deadline | Молчащий backend замечает закрытие UDS, active_requests и synthetic token reservations возвращаются к нулю, группа остаётся READY |
| Полный text API | Chat/completions JSON/SSE, plaintext/encrypted, постепенная выдача SSE, корректные content и usage |
| Worker deadline | Начавшийся SSE прерывается без `[DONE]`, worker/proxy/client считают один failure с нулевой оплатой |
| Гибель member backend | После первого SSE event убивается rank 1; оба активных потока epoch, один encrypted, прерываются без успешного terminal/billing |
| Пауза member-agent/control loss | Останавливается сам member-agent; control/lease watchdog закрывает группу и оба потока; после SIGCONT выполняется recovery |
| Recovery | Worker и его реклама в proxy становятся disabled, gate возвращает 503 и не принимает новые запросы; затем новый epoch, новые backend PID, старые PGID/sockets отсутствуют, synthetic state пуст; новый запрос оплачивается один раз |
| Cleanup | Нет процессов собственного Cocoon стенда, backend sockets и engine namespaces; порты освобождены, root firewall неизменён |

Для каждого сквозного success/failure сверяются counters трёх ролей, шесть token/payment/balance значений и освобождение proxy reservations. Дополнительная поздняя выборка исключает повторный terminal/billing. При каждом отказе одновременно обслуживаются два запроса; после восстановления проверено, что новый backend выполнил только собственный warmup, не имеет активных запросов и зарезервированных prompt tokens.

| Отказ → оба HTTP-потока завершились ошибкой | Mac | Linux WG |
|---|---:|---:|
| SIGKILL backend member | 0,046 с | 0,029 с |
| SIGSTOP member-agent | 1,484 с | 1,559 с |

Это измерения конкретных прогонов. В Mac журнал фиксирует `control lease expired`, в Linux — `TLS/frame deadline exceeded`: transport watchdog может сработать раньше пятисекундной WG lease. Гарантия не основана на одинаковой задержке всех видов отказа. Gate закрывается локально, а disabled распространяется monitor polling.

## Регрессии и артефакты

- 16 локальных supervisor/API тестов и 13 group/membership сценариев прошли, включая C++ profile и protocol checks.
- `python3 test/test-worker-cancellation.py`: 17 C++ cancellation cases / 207 запросов, 10 HTTP framing cases и 74 сквозных запроса прошли на обычном single-worker backend. В тесте исправлено ожидание предпосылки сохранённого баланса: 15 с было меньше рабочего интервала payment poll 10–20 с; теперь 25 с с учётом DB flush 1–2 с. Бюджеты проверки самой отмены не увеличены. Первый прогон завершился именно на этом ожидании; повторный прошёл полностью.
- Обычный `benchmark/smoke-local.py --scenario normal` прошёл: восемь JSON/SSE chat/completions запросов, включая четыре encrypted, со штатным ненулевым billing. C++ framing/postprocessor tests также прошли.

Финальные Mac/Linux результаты, конфиги и логи сохранены в [`build/step9-evidence-20261004`](../build/step9-evidence-20261004): `mac-worker-final/` и `linux-worker-final/` с `result.json` и `source-sha256.json`; hashes реализации и финального integration test сверены с текущими файлами. `mac-group/` и `mac-supervisor/` содержат отдельные регрессии. Предыдущие успешные проверки одного типа member failure оставлены как `mac-worker/` и `linux-worker/`. Дополнительные логи: [`build/step9-worker-regression-r2`](../build/step9-worker-regression-r2) и [`build/step9-normal-regression`](../build/step9-normal-regression). Всё находится в ignored build tree, не включено в коммит.

При первом Linux запуске полный стенд не стартовал из-за отсутствующего `cocoon-subst`: VM ранее использовалась для agent/network тестов. Команда приёмки теперь явно собирает `cocoon-subst` и `router`. После успешных прогонов независимая проверка показала пустые `/run/netns` и `/run/cocoon-pipeline-net`, отсутствие agent/helper/backend процессов. Root firewall сравнивается самим fixture до и после теста.

## За скобками

Реальные SGLang/vLLM, распределённая математика, GPU/KV cleanup и производительность относятся к шагам 10–11/13. Simulator доказывает очистку процессов/сокетов/резерваций, а не аппаратного KV-cache. Очереди полного worker → proxy пути остаются в шаге 5. P8-01/P8-02 и P7-02 сохраняют работы по egress, правам, measured image и cleanup всего дерева при гибели агента. Это не тест двух CVM и не confidential deployment.

Новый P9-01 фиксирует отсутствие совместной валидации конфигов worker и agent: model name/capacity/coefficient пока задаются раздельно, тест и инструкция используют согласованные значения. Конкретная задача генерации и отрицательных deployment checks записана в [плане](../pipeline-plan.md#за-скобками-реализации-шага-9). P3/P4/P7/P8 не закрываются этим результатом; в частности, нет нового внешнего TL cancel, полного proxy crash reconciliation или production attestation.
