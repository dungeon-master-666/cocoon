# Шаг 11: адаптер vLLM

Дата: 2026-10-04. Статус: выполнен. Оба vLLM profiles прошли отдельную приёмку через Cocoon на двух RTX.

## Реализация

Добавлены два закреплённых Qwen3 text profiles и `VllmAdapter`, использующий
существующую группу, WireGuard и gate. Proxy/worker API не меняются. vLLM
`0.29.0+cu129`, образ `vllm/vllm-openai:v0.29.0-cu129@sha256:7ef5a35d1ef8ce2cf9d671dd91eec6e367c5849262e0362b4d3d4a26be0d87d2`.
Модели и manifests общие со SGLang, проверяются на read-only mount до запуска.

PP=2, TP=1, BF16, context до 4096, одна последовательность, eager execution,
без CPU offload и prefix cache. Rendezvous: `10.231.0.1:29501`; NCCL/Gloo только
по `wg0`; API head только `127.0.0.1:30000`; member — headless. Compute RPC timeout
30 с. Head warmup проходит через всю модель; живой launcher member сам по себе
не открывает gate. На каждом rank helper от UID 65534 владеет деревом процессов.

Общий `engine-helper.py` сохраняет исправления HTTP framing и backpressure шага 10.
Приватный vLLM middleware отменяет активную ASGI-задачу, затем вызывает адресный
engine abort. Ограниченный набор отметок отмены предотвращает позднюю регистрацию.
Идентификаторы задаёт helper; beam search, routing, LoRA и KV transfer extensions
не допускаются. Административный API не доступен через UDS/gate.

Семантика cancellation привязана к закреплённой версии:
[OpenAI chat IDs](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/entrypoints/openai/chat_completion/serving.py),
[completion IDs](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/entrypoints/openai/completion/serving.py),
[AsyncLLM abort](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/engine/async_llm.py).

## Воспроизведение

```bash
python3 test/test-pipeline-vllm.py --build-dir build/local
python3 test/test-pipeline-sglang.py --build-dir build/local
python3 test/test-pipeline-worker.py --build-dir build/local
python3 benchmark/smoke-local.py --build-dir build/local --skip-build --scenario normal
python3 test/test-pipeline-vllm-gpu.py --prepare
```

Native suite переиспользует SGLang HTTP contract и добавляет vLLM-specific
проверки. GPU suite тот же: JSON/SSE, encrypted/plain, chat/completion, usage и
billing, длинный prefill, disconnect/deadline, member failure, новый epoch и
оплачиваемый recovery. Запуск и cleanup изолированы в `cocoon-step11` на двух
хостах из `experiments/gpu-pipeline/lab.json`.

GPU-команда требует свободных GPU, SSH с passwordless sudo, Docker/NVIDIA runtime
и read-only snapshots моделей, подготовленных в [шаге 2](../experiments/gpu-pipeline/README.md).
`--prepare` передаёт исходники и собирает dev-образы/Cocoon; затем проверяет обе
модели и выполняет cleanup даже при ошибке теста. Повтор на подготовленном стенде:
`python3 test/test-pipeline-vllm-gpu.py`. Команда возвращает ненулевой код при
ошибке API, отмены, восстановления, сверки artifacts или cleanup.

## Результаты

**Нативный Mac:** 28 vLLM tests, 22 SGLang tests, 17 групп worker integration — PASS.
Артефакты: `build/step11/native-contract`, `build/step11/sglang-contract`,
`build/step11/worker-regression`. Source hashes контрактных прогонов совпадают
с текущими исходниками. Проверяются JSON/SSE и framing, backpressure, disconnect,
последовательное admission, fail-closed, private endpoints, pinned artifacts,
инкрементальная пересборка manifest; отдельно vLLM task/engine cancellation,
отмена до регистрации, ошибки подтверждения и headless member.

Обычный worker без pipeline также прошёл `smoke-local.py --scenario normal`:
8 вариантов JSON/SSE, chat/completion, encryption, billing и cleanup на тестовом
backend. Артефакты: `build/step11/direct-worker-smoke`; это регрессия обычного
пути worker/API, а не новый single-GPU benchmark. Все четыре примера vLLM
конфигураций принимаются `pipeline-agent-dev --check-config`.

**Две RTX 4000 SFF Ada, 20475 MiB каждая:**

| Profile | Прогон / результат | Сценарии | GPU head/member после recovery |
|---|---|---:|---:|
| Qwen3-0.6B | [small-985e8603b5d7 — PASS](../build/step11/gpu/small-985e8603b5d7/result.json) | 13 | 18248 / 18272 MiB |
| Qwen3-14B | [large-9abd024257a7 — PASS](../build/step11/gpu/large-9abd024257a7/result.json) | 13 | 17758 / 17840 MiB |

Это снимки памяти живой модели с зарезервированным KV-кэшем, не измерение пика.
После stop в обоих прогонах обе GPU вернулись к **2 MiB**; GPU-процессов,
namespace и owner records не осталось, root firewall не изменился.

На обоих ranks совпадают vLLM `0.29.0+cu129`, PyTorch `2.13.0+cu129`, CUDA `12.9`,
driver `595.91.07`. Фактически загруженный NCCL по INIT-логам — `2.30.7+cuda12.9`;
PyTorch отдельно сообщает версию `2.29.7`. Логи подтверждают PP ranks 0/1 и
`NCCL_NET=Socket` через `wg0`; в engine namespaces только `lo` и `wg0`.
UID backend — 65534 без capabilities. SHA-256 42 исходных файлов и тестовых
компонентов на каждом host совпадает с checkout; agent/sandbox binaries обоих
ranks одинаковы. Полные версии, hashes, конфигурации, статусы и логи сохранены
рядом с результатами в `evidence-0.json`, `evidence-1.json` и `rank-*/`.

Модели: Qwen3-0.6B revision `c1899de289a04d12100db370d81485cdf75e47ca` (14+14
слоёв), Qwen3-14B revision `40c069824f4251a91eefaf281ebe4c544efd3e18` (20+20).
Оба прогона: PP=2/TP=1, BF16, context 4096, одна последовательность, CPU offload 0.

На каждой модели прошли восемь API-вариантов и ещё пять сценариев: prefill на
2062 prompt tokens; disconnect с освобождением KV без смены epoch; Cocoon deadline
с нулевой оплатой; успешный оплачиваемый запрос после отмены; потеря member во
время encrypted SSE с новым epoch и оплачиваемым recovery. После отмены helper
не имеет активных запросов, а running/waiting/KV gauges возвращаются к нулю;
подтверждения abort успешны. При потере member клиент получает обрыв без `[DONE]`,
оплата равна нулю, gate закрыт до нового полного warmup. Старые процессы и sockets
удалены, новый запрос после восстановления оплачивается по фактическому usage.

При подготовке чистой Linux-сборки явно включены `TON_USE_ROCKSDB` и
`TDDB_USE_ROCKSDB`; флаг vLLM для логов запросов соответствует закреплённому CLI:
`--no-enable-log-requests`. Первый GPU-прогон `small-8760dc017576` подтвердил
API, длинный prefill, disconnect/deadline и освобождение KV, но не принят:
512-токенный fault-запрос завершился за 3,3 с, раньше удалённой команды остановки.
Тест теперь запрашивает 3000 токенов и проверяет активность запроса до инъекции,
сохраняя обязательную проверку обрыва активного SSE без `[DONE]` и нулевой оплаты.
При сборе артефактов исключены
Unix sockets/devices, которые нельзя воспроизвести в длинных путях на Mac.
Неуспешные отчёты сохранены; cleanup обоих hosts в них прошёл. Повтор
`small-e2bd2beb4af3` дополнительно подтвердил, что Cocoon отклоняет engine extension
`ignore_eos`: он оставлен только в прямом gate-тесте, а fault-тест использует
обычный публичный API без изменения схемы worker.

## За скобками

Deployment/bundles — шаг 12, длительная нагрузка и performance benchmark — шаг 13;
общие ограничения очередей Cocoon — отложенный шаг 5. P7-02, P8-01/P8-02, P9-01,
P4-01/P4-02, P3-02, P7-01/P7-03/P7-04 сохраняются: гибель самого agent/helper,
service egress/изоляция/упаковка, согласование worker capacity, внешняя TL cancel,
reconciliation, encryption readiness, production trust и ротация.

У vLLM один scheduler/block allocator на head для всего PP. Отдельные KV gauges
headless rank не существуют; подтверждение освобождения логических KV-блоков
основано на shared scheduler metrics, а физической памяти — на остановке обоих
rank. Резервирование GPU memory при живой модели ожидаемо. RTX/dev-контейнеры не
доказывают CC/CVM-конфиденциальность. Другие модели/версии, batching, LoRA и
оптимизации требуют отдельного решения о scope и квалификации.

Новых работ вне плана не выявлено. Обязательные критерии шага 11 подтверждены
отдельными vLLM прогонами; открытые работы прежних шагов сохраняются.
