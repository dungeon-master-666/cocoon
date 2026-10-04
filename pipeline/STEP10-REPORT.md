# Шаг 10: адаптер SGLang

Дата: 2026-10-04. **Шаг выполнен.** Контрактные проверки, регрессия Cocoon и полная
GPU-приёмка Qwen3-0.6B/Qwen3-14B на двух RTX прошли, включая recovery и cleanup.

## Реализация

`Sglang.cpp` реализует существующий `BackendAdapter`. Membership, WireGuard и
gate переиспользуются; TL, proxy и worker для шага 10 не менялись. Агент выбирает
adapter по доверенному профилю, а simulator-only аргументы больше не передаются
реальному backend.

Поддерживаемые dev-профили: `sglang-qwen3-0.6b-dev-pp2-wg-v1` и
`sglang-qwen3-14b-dev-pp2-wg-v1`. SGLang закреплён на образе
`lmsysorg/sglang:v0.5.10.post1-cu130-runtime@sha256:715c461258624eae38124dcb1e1f620cbe307594fd3403ab92caf1b7017afd0f`.
Qwen3-0.6B revision `c1899de289a04d12100db370d81485cdf75e47ca`, Qwen3-14B revision
`40c069824f4251a91eefaf281ebe4c544efd3e18`; manifests перенесены из шага 2 и
включены в бинарник через `sglang-models.json`.

PP=2, TP=1, BF16, одна GPU и одна активная последовательность на rank. Приёмка
использует context/max_total_tokens=4096. Схема допускает уменьшение context до
512 и token budget до context, но меньшие варианты не составляют отдельную GPU
benchmark-матрицу. Prefix cache, CUDA graphs, overlap и chunked prefill выключены;
CPU weight offload не включён. Последнее существенно для 14B, чьи BF16 веса
не помещаются в одну RTX этого стенда. Настройки и граница старой ошибки chunked
prefill объяснены в [отчёте шага 2](../experiments/gpu-pipeline/REPORT.md).

Пути модели, argv/env и внутренний endpoint не задаются runtime JSON. Helper
проверяет версию SGLang, read-only mount и точный набор/размеры/SHA-256 файлов до
запуска. Он работает от UID 65534 без capabilities в engine namespace `lo + wg0`.
NCCL/Gloo закреплены за `wg0`, SGLang HTTP — за `127.0.0.1:30000`. Head helper
предоставляет filesystem Unix socket для chat/completions; member — только
отдельный health socket. Admin API и metrics через gate не публикуются.

Member health означает локальную готовность. Head warmup выполняет настоящую
генерацию через всю модель; группа открывает gate после обеих проверок. Startup
budget — 900 с, warmup — 90 с, formation — 1200 с, health watchdog агента — 8 с,
compute watchdog SGLang — 30 с. Остановка: 10 с до финального SIGKILL и 5 с на
подтверждение; retries — две попытки после первой.

Helper ограничивает headers/body до 8 KiB, wire response до 2 MiB, соединения до
16 и inference до одного запроса. Он пересылает порциями 16 KiB с backpressure,
наблюдая disconnect одновременно с чтением upstream и записью downstream.
Gate дополнительно сохраняет свой лимит body ответа 1 MiB и deadline до 120 с.
Поддерживается одна текстовая генерация, `n=1`; batches и расширения LoRA/session/
backend routing отвергаются.

Полный HTTP-ответ определяется по `Content-Length` либо последнему chunk вместе
с trailers; EOF нужен только при отсутствии явного framing. Передав последние
байты ответа в ограниченный буфер отправки, helper освобождает admission до
ожидания drain/закрытия соединений. Отключение gate после полного ответа не
вызывает отмену. SSE `[DONE]` без завершённого HTTP framing недостаточно.

При disconnect до конца ответа helper закрывает upstream и вызывает `/abort_request` с собственным
уникальным `rid`, повторяя адресную отмену через 100 ms для регистрации в полёте.
Клиентский `rid` заменяется. Ошибка подтверждения отмены закрывает admission и
приводит к отказу backend/группы. Ответ abort=200 сам по себе не считается
доказательством освобождения GPU: это отдельно проверяется метриками обоих ranks.

Linux subreaper владеет деревом SGLang. При управляемой остановке и неожиданном
выходе launcher он завершает и reap-ит потомков, включая сменивших process group.
Это не закрывает P7-02 о внешнем cgroup/supervisor при гибели самого агента/helper.

## Проверки и воспроизведение

```bash
python3 test/test-pipeline-sglang.py --build-dir build/local
python3 test/test-pipeline-worker.py --build-dir build/local
python3 test/test-pipeline-sglang-gpu.py --prepare
# Повтор на подготовленном стенде с теми же исходниками:
python3 test/test-pipeline-sglang-gpu.py
```

Нативные проверки не требуют SGLang/GPU, но требуют TCP/Unix sockets. GPU-команда
использует уже согласованные два хоста из `experiments/gpu-pipeline/lab.json` и
модели шага 2. `--prepare` передаёт явный список исходников, собирает dev-образ и
Cocoon через Clang/Release/x86-64. Скомпилированные agent/sandbox одинаковы на обоих
хостах. Исходники и build runtime монтируются read-only при запуске теста; модели
тоже read-only. Сборка отдельно использует writable source mount для генераторов TL.

Изолированный test underlay получает проверяемые на конфликт адреса
192.168.100.12/13 через ipvlan. Каждый настоящий агент сам создаёт engine namespace,
WireGuard и firewall своего underlay. Cocoon client/proxy/worker/key-manager с fake
TON находятся на loopback head underlay; member не запускает второй worker.
Тест отказывается занимать GPU с существующей compute-нагрузкой.

Это dev-контейнеры с расширенными правами agent и host PID namespace для проверки
исходного network namespace; SYS_PTRACE нужен для чтения `/proc/1/ns/net`.
Backend сбрасывает все capabilities и UID. Это не production PID/device/mount
sandbox, не CVM и не проверка GPU CC. Dev-образы добавляют build/network tools к
одному закреплённому inference base; их собственные image IDs могут различаться.
Упаковка неизменяемого deployment остаётся в шаге 12/P8-02.

## Результаты

**Mac:** C++ profile/launch-plan checks и 15 Python helper tests прошли. Проверены
JSON/HTTP errors, incremental SSE, backpressure и продолжение после паузы,
disconnect до headers и при заблокированной записи, адресная отмена и fail-closed,
конкурентное admission, framing/лимиты/allowlist, member-only health и неверные
model artifacts. Финальная регрессия шага 9 прошла: 17 групп проверок, включая два
одновременных потока при backend/control failure, нулевую оплату ошибок и recovery.
SHA-256 исходников обоих наборов сверены с файлами на момент первичной приёмки,
до последующих исправлений HTTP helper и CMake.

**GPU Qwen3-0.6B: PASS. GPU Qwen3-14B: PASS.** Всего 26 запросов через Cocoon:
22 успешных и четыре неуспешных, десять encrypted; дополнительно две отмены direct gate.

На каждой модели проверяется 13 сценариев: 13 запросов через Cocoon (11 успешных,
два неуспешных, пять encrypted) и отдельный direct-gate disconnect. Warmup запросы
не включены в эти числа. Сценарий member failure включает неуспешный поток и
отдельный успешный запрос после восстановления.

| Сценарий | Критерий |
|---|---|
| Один worker | Одна worker connection в proxy с правильным API model; member не имеет gate |
| 8 сочетаний text API | Chat/completions × JSON/SSE × plain/encrypted; непустой результат, один finish/DONE, incremental SSE, actual usage |
| Оплата | Шесть token/balance значений и terminal counters client/proxy/worker; success оплачивается ровно один раз, failure — ноль, reservations освобождены |
| Длинный prefill | 2062 prompt tokens, больше старой границы 2048; после ответа реальные KV slots свободны |
| Direct gate disconnect | Сначала положительный `sglang:num_used_tokens` на обоих rank, затем ноль running/queued/used tokens, helper cancellation, тот же epoch |
| Worker deadline | Начавшийся SSE обрывается без DONE, без оплаты; KV обоих ranks освобождён, следующий запрос успешен |
| Member launcher SIGKILL | Активный encrypted поток прерывается без успешного terminal/оплаты; worker/proxy disabled, gate models/inference возвращает 503 |
| Восстановление | Новый общий epoch, новые процессы; старые process groups и sockets обоих rank отсутствуют; новая генерация оплачена один раз |
| Остановка | Agent cleanup, пустые namespace/owner records, нет GPU процессов, память вернулась к baseline, root firewall неизменён |

KV проверяется по точному `sglang:num_used_tokens` вместе с running/queue gauges,
а не только округлённому `token_usage`. Idle gauges этой версии SGLang могут
обновляться раз в 30 с, поэтому тест ждёт до 40 с: это предел наблюдения, а не
измеренное время фактической очистки. CUDA allocator может сохранять зарезервированную
память при живой модели; возврат всей GPU-памяти проверяется после остановки процессов.

| Финальный прогон | Qwen3-0.6B | Qwen3-14B |
|---|---:|---:|
| Сценарии | 13 PASS | 13 PASS |
| Оплаченные adjusted tokens | 2466 | 2466 |
| Cost units (тариф 2, coefficient=1000) | 4932 | 4932 |
| Оплата каждого из двух failures | 0 | 0 |
| Занятые KV tokens head/member до disconnect | 23 / 66 | 21 / 56 |
| KV tokens после отмены | 0 / 0 | 0 / 0 |
| GPU memory head/member после recovery, MiB | 1416 / 1416 | 14878 / 14878 |
| GPU memory после stop, MiB | 2 / 2 | 2 / 2 |

В обоих прогонах после stop отсутствовали compute processes, engine namespaces
и owner records; host firewall сохранился. Суммарно оплачено 4932 adjusted tokens
и 9864 cost units. Это функциональные замеры, benchmark производительности
остаётся в шаге 13.

Оба хоста: RTX 4000 SFF Ada 20475 MiB, driver 595.91.07, SGLang 0.5.10.post1,
Torch 2.9.1+cu130, CUDA 13.0. `torch.cuda.nccl.version()` сообщает 2.27.7; отдельно
NCCL-логи SGLang сообщают 2.28.3+cuda13.0 и `NET/Socket ... wg0`. Эти сведения
не смешиваются в одну версию. В обоих прогонах версии и бинарники между ranks
совпали; source hashes, UID/capabilities, interfaces и WG также проверены.

## Артефакты и исправленные проблемы стенда

- `build/step10/review-fixes`: сборка C++ targets, актуальные launch plans и
  22 успешных контрактных теста после исправлений HTTP helper/CMake ниже.
- `build/step10/native-contract-r4`: 15 tests, C++ plans, source hashes.
- `build/step10/simulator-regression-final`: 17 групп, полный Cocoon regression, source hashes.
- `build/step10/gpu/small-1c04981ca10d`: успешная приёмка 0.6B, `result.json`,
  `evidence-{0,1}.json`, per-rank agent/backend/Cocoon logs и cleanup.
- `build/step10/gpu/large-19837547a8f9`: успешная приёмка 14B с тем же набором evidence.
- `build/step10/gpu/build.log`, `image-{0,1}.log`, `transfer-files.txt`: подготовка.
- `build/step10/final-verification.json`: итоговая сверка успешных результатов и
  SHA-256 исходников первичной приёмки; `gpu/final-host-audit.json` — отсутствие тестовых
  контейнеров/GPU-процессов и baseline 2 MiB после обоих прогонов.

Raw artifacts лежат в ignored build tree. В ранних попытках исправлены ошибки
сценария: имена CMake targets, writable TL generation, повторная конфигурация
secp256k1, выбор Clang вместо GCC для текущего TON, права dev-container и дублирующийся
аргумент host rank. Реальный API выявил допустимый null/отсутствующий cached-token
details при выключенном radix cache и запрет `ignore_eos` публичным валидатором
Cocoon. Проверки учитывают первый случай и используют обычный длинный prompt во
втором; публичный API не расширялся. После неуспешных GPU-попыток cleanup тоже
подтвердил освобождение ресурсов и неизменность host firewall.

## Исправления после review (2026-10-04)

Исправлена ложная отмена при отключении gate после полного HTTP-ответа, когда
движок ещё не закрыл TCP. Helper учитывает `Content-Length`, chunked framing и
trailers, освобождает слот при передаче последних байтов и не отменяет уже
завершённую генерацию при отключении во время финального drain. Ошибочное или
оборванное framing вызывает адресную отмену; streaming и backpressure сохранены.

`sglang-models.json` добавлен в `CMAKE_CONFIGURE_DEPENDS`. Изменение manifest
автоматически запускает конфигурацию CMake, обновление заголовка и пересборку
зависимого кода. Регрессионный тест использует настоящий `pipeline/CMakeLists.txt`
во временной копии проекта: меняет только JSON, запускает обычный `cmake --build`
и проверяет новые данные в скомпилированной программе.

Локальная сборка `test-pipeline-sglang`, `test-pipeline-profile` и
`pipeline-agent-dev` успешна. C++ profile/launch-plan checks и все 22 Python tests
прошли, включая семь новых проверок: задержанный EOF после JSON/SSE, фрагментация
framing, отключение во время финального drain, настоящие отмены до конца ответа,
неверные/обрезанные ответы и лимиты, close-delimited ответы и повторная сборка.
Дополнительно подтверждено наличие JSON в графе регенерации основного Ninja build.
GPU-приёмка после этих исправлений не повторялась; GPU-результаты выше относятся
к первичной версии шага 10.

## За скобками

Шаг 11 — vLLM; шаг 12 — bundles/deployment; шаг 13 — длительная нагрузка и
сравнительные benchmarks. Шаг 5 остаётся отдельной работой по очередям всего
Cocoon transport. Открыты прежние P7-02, P8-01/P8-02 и P9-01: agent crash cleanup,
служебный egress, права/упаковка и согласованная конфигурация worker/agent.
P4-01 не позволяет обещать немедленный cancel от внешнего HTTP-клиента через TL.
P3-02, P4-02, P7-01/P7-03/P7-04 сохраняют readiness encryption, billing reconciliation,
production trust, drain/rotation и отзыв policy. Полный перечень и критерии —
[план](../pipeline-plan.md#за-скобками-реализации-шага-10).

Другие модели/версии, более двух ranks, concurrency > 1, batching, LoRA,
quantization, prefix cache и оптимизации scheduling требуют отдельной квалификации
и решения о scope. RTX dev-приёмка не является confidential production.

Новых работ вне плана не выявлено; перечисленные открытые записи сохраняются.
