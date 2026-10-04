# Шаг 4: отмена HTTP и deadlines

Дата: 2026-10-04. Проверка на нативном macOS arm64, `build/local`, синтетический backend и fake-TON. Реальные GPU/CVM не использовались.

## Реализация

- `run_http_request` возвращает копируемый слабый `HttpRequestHandle`. Потокобезопасная идемпотентная отмена работает до начала I/O, во время connect/write/read и после завершения. Игнорирование возвращаемого handle сохраняет прежний режим работы вызывающего кода.
- Сессия выполняет I/O, timer и terminal callbacks на одном Asio strand. Отдельный абсолютный deadline включает очередь перед стартом, запись запроса, чтение headers и всего body; непрерывные chunks не продлевают его. Дробные секунды сохраняются. Неположительные, бесконечные и NaN timeout отклоняются до открытия сокета. API уже принимает числовой `IPAddress`, поэтому DNS resolver исключён из этого пути.
- До terminal callback отменяется timer и закрывается socket. Удалён цикл владения сессии собой; callback освобождается сразу после terminal, оставшиеся отменённые I/O handlers освобождают сессию. После terminal новых callbacks нет.
- Worker владеет активными request actors в registry `(connection_id, request_id)`. `WorkerProxyConnection::pre_close` отменяет связанные запросы. Запись и admission slot освобождаются при подтверждении завершения конкретного actor; позднее завершение не может удалить другой actor с повторно использованным ID. Повтор активного ID закрывает неоднозначное соединение.
- Request actor отменяет upstream при любом завершении/ошибке и при teardown. Его alarm учитывает время с создания actor, HTTP получает оставшийся бюджет. Deadline проверяется и при обработке ответов, чтобы уже просроченные queued callbacks не стали успехом. Ошибка имеет нулевое расчётное usage.
- `/jsonstats` worker показывает `stats.active_requests`. Monitor сохраняет handle, отменяет его при teardown; после transport failure продолжает проверки readiness.

## Воспроизведение

Из корня репозитория с настроенным CMake build шага 1:

```bash
python3 test/test-worker-cancellation.py --build-dir build/local
python3 benchmark/smoke-local.py --scenario all
```

Первая команда собирает runners и два transport-теста, затем поднимает локальные client/proxy/worker/key-manager/router. `--skip-build` использует готовые binaries. `--output-dir` задаёт новый каталог для логов, результатов и SHA-256 исходников. Только процессы собственного стенда получают сигналы; GPU-хосты не используются.

## Результаты проверки отмены

`test-http-cancel`: **17 случаев / 207 запросов**, включая четыре I/O-потока, отмену до старта, истечение deadline в очереди, невалидные timeout, отмену внутри header/body callbacks, зависшие headers/body/write, непрерывный trickle, отмену после success, 64 гонки completion/cancel и 128 одновременных отмен. Проверены ровно один terminal, отсутствие поздних callbacks, уничтожение callback и сессии, наблюдаемое peer EOF/reset. Socket cleanup подтверждается до остановки fixture, в пределах двух секунд. Также прошли 10 прежних framing/transport случаев.

Сквозной тест: **74 запроса** — 70 неуспешных и четыре успешных. После каждой серии registry и активные backend-сокеты равны нулю; все 70 закрытий наблюдены самим backend, а не получены остановкой fixture. Ни один backend handler не дожил до собственного предельного ожидания.

| Сценарий | Наблюдение |
|---|---|
| Зависание до headers, после headers/токена; непрерывный trickle | При пользовательском timeout 0,8 с освобождение за 0,729–0,735 с; сохраняются существующие коэффициенты бюджета 0,95 в client и proxy |
| Malformed SSE / backend error с незавершённым HTTP body | Освобождение сразу после ошибки, без ожидания заданных 30 с; отдельные запросы — менее 0,009 с |
| Три серии по 8 timeout и 8 postprocessing errors | После каждой серии ноль активных actors/sockets, нет повторного terminal, оплаты и reservations |
| Два падения proxy по 8 активных запросов с timeout 30 с | Живой worker закрыл sockets за 0,108 и 0,110 с, не увеличил successful usage; registry пуст |
| Успех до/после отмен и после каждого restart proxy | Тот же worker обрабатывает новый запрос; каждый успех учитывается ровно один раз по 134 adjusted tokens, `total_cost=268` |
| Ошибка первой проверки `/v1/models` | WorkerUplinkMonitor повторяет probe и восстанавливает readiness |
| Финальный cleanup стенда | Все созданные process groups остановлены, порты освобождены |

Для timeout/postprocessing batches сверяются counters worker/proxy/client, шесть token/payment/balance значений и освобождение proxy reservations. Для падения самого proxy измеряется живой worker и backend; сквозное состояние погибшего процесса не объявляется проверенным. После restart снова проверяется обычный запрос со всеми шестью расчётными значениями.

При fault injection приостанавливается только Python supervisor, чтобы он не остановил worker вслед за убитым proxy. После измерения тот же worker переподключается к новому proxy. В конце supervisor возобновляется и успевает reap погибший child перед cleanup. Его сообщение `proxy exited with -9` ожидаемо для этого теста.

## Регрессия шага 3

Полный прогон `python3 benchmark/smoke-local.py --skip-build --scenario all --output-dir build/step4-regression` прошёл: **23 сценария / 37 запросов, включая девять encrypted**. 12 успешных запросов оплачены по 134 adjusted tokens, 25 ошибок — по нулю. Проверены обычные JSON/SSE, задержки, HTTP/backend errors, обрывы до/после headers, usage и `[DONE]`, malformed/неполные ответы, шифрование, отсутствие двойного завершения, reservations и cleanup. C++ postprocessor/framing tests, запускаемые этой командой, также прошли.

Логи, per-request accounting и SHA-256 исходников: [`build/step4-regression`](../build/step4-regression). Исходники реализации между проверкой отмены и регрессионным прогоном не менялись.

## Артефакты и границы

Успешный прогон: [`build/step4-acceptance-r2`](../build/step4-acceptance-r2), включая `result.json`, `source-sha256.json` и C++ test logs. Каталог ignored и не входит в коммит.

Первый прогон сохранён в `build/step4-acceptance`: отмена прошла, но аварийный restart proxy до сохранения последней оплаты воспроизвёл `skipping extended tokens compare: not implemented yet` и повторяющийся `Wrong constructor found at 4`. Этот незавершённый путь существовал до шага 4 в `ProxyInboundWorkerConnection`; он не исправляется изменением отмены worker. Основной тест восстановления явно ждёт `earned_tokens_committed_to_proxy_db == earned_tokens_max_known` перед crash. Это ограничение приёмки восстановления, не обход проверки отмены: в обоих прогонах backend-сокеты закрылись сразу. Незавершённое reconciliation зафиксировано как P4-02 в плане. Также в первом прогоне обнаружена гонка cleanup тестового supervisor на macOS; оставшийся тестовый router остановлен вручную, порядок cleanup исправлен в fixture, успешный прогон не оставил процессов.

Освобождение HTTP и request actors не доказывает освобождение GPU/KV реального backend. Это проверяется в шагах 10–11/13; group failure/gate — шаг 9; пределы памяти и mailbox — шаг 5. Внешний HTTP disconnect клиента не добавляет нового TL request-cancel (P4-01). Границы времени измерены при работающих scheduler/event loop, не являются гарантией жёсткого реального времени при перегрузке или остановке процесса. Тесты registry проходят через реальные запросы/соединения; произвольный malicious TL replay не входит в этот стенд.
